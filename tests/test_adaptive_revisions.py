"""The revision lifecycle rules — PLN-04..PLN-08, NXT-19 + NXT-20 (review edf938c).

``test_adaptive_contracts.py`` proves the SHAPES parse; these tests prove
the RULES: tactical revisions land only inside the transformations the
contract's policy explicitly pre-approved (authority from the contract
alone, never from the old plan's grants, unknown materiality routed to a
decision), approvals compare-and-swap on their epoch AND bind to the
exact proposed revision and current work — activation is one conditional
transaction or a typed refusal that consumes nothing. Questions block
instead of defaulting, evidence is superseded precisely rather than
deleted, and decision records stay secret-free.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from forge.adaptive.models import PlanRevision, WorkContract
from forge.adaptive.revisions import (
    ActivationRecord,
    ActivationRefused,
    ActivationSession,
    ActivePlanState,
    Question,
    RevisionDecision,
    TacticalPolicy,
    activate_revision,
    apply_tactical,
    change_log,
    classify_revision,
    decision_record,
    fresh_session_brief,
    invalidation_set,
    parse_tactical_policy,
    plan_digest,
    proposed_revision_identity,
    route_question,
    stale_callback_guard,
    transformation_kinds,
)

D_CONTRACT = "1" * 64
D_SNAPSHOT = "2" * 64
D_STALE = "9" * 64
#: The maximally explicit policy: every automatic transformation granted,
#: so the classic tactical flows stay testable. Narrower policies are
#: what the NXT-20 fail-closed tests exercise.
POLICY_ALL = "reorder,edit_step,reword,add_step,remove_step"


def _contract(**overrides) -> WorkContract:
    base = {
        "schema": "forge.proposal.work-contract/1",
        "work_id": "wp-demo-1",
        "contract_revision": 1,
        "objective": "Add order reservation expiry without duplicate billing.",
        "read_scope": [
            {"repository_id": "orders", "paths": ["**"]},
            {"repository_id": "billing", "paths": ["**"]},
        ],
        "write_scope": [{"repository_id": "orders", "paths": ["src/**", "tests/**"]}],
        "allowed_effects": ["discovery_read", "candidate_publication", "verification_run"],
        "acceptance": [
            {"id": "AC-1", "description": "Expiry handled idempotently.", "required": True}
        ],
        "tactical_revision_policy": POLICY_ALL,
    }
    return WorkContract.model_validate(base | overrides)


def _step(step_id: str, objective: str, **overrides) -> dict:
    base = {
        "step_id": step_id,
        "objective": objective,
        "write_repository_id": None,
        "depends_on": [],
        "evidence_refs": [],
        "acceptance_refs": ["AC-1"],
        "impact": ["internal"],
    }
    return base | overrides


def _revision(steps: list[dict], revision: int = 1, **overrides) -> PlanRevision:
    base = {
        "schema": "forge.proposal.plan-revision/1",
        "plan_id": "plan-demo-1",
        "work_id": "wp-demo-1",
        "revision": revision,
        "parent_revision": None,
        "work_contract_digest": D_CONTRACT,
        "snapshot_set_digest": D_SNAPSHOT,
        "summary": "Inspect, then implement inside the approved scope.",
        "steps": steps,
    }
    return PlanRevision.model_validate(base | overrides)


def _base_steps() -> list[dict]:
    return [
        _step("S1", "Inspect existing idempotency behavior."),
        _step(
            "S2",
            "Implement the authorized Orders change.",
            write_repository_id="orders",
            depends_on=["S1"],
        ),
    ]


def _decision(proposed: PlanRevision | None = None, **overrides: Any) -> RevisionDecision:
    base: dict[str, Any] = {
        "decision_id": "rd-1",
        "work_id": proposed.work_id if proposed is not None else "wp-demo-1",
        "parent_revision": 1,
        "proposed_revision_id": (
            proposed_revision_identity(proposed) if proposed is not None else "plan-demo-1#2"
        ),
        "proposed_digest": plan_digest(proposed) if proposed is not None else "8" * 64,
        "work_contract_digest": D_CONTRACT,
        "authorization_epoch": 3,
    }
    return RevisionDecision(**(base | overrides))


def _approved(proposed: PlanRevision, **overrides: Any) -> RevisionDecision:
    """An approved decision correctly bound to ``proposed``'s exact content."""
    pending = _decision(proposed, **overrides)
    return pending.approve("approver", pending.authorization_epoch)


def _current(**overrides: Any) -> ActivePlanState:
    base: dict[str, Any] = {
        "work_id": "wp-demo-1",
        "plan_id": "plan-demo-1",
        "active_revision": 1,
        "work_contract_digest": D_CONTRACT,
        "authorization_epoch": 3,
        "publication_epoch": 7,
    }
    return ActivePlanState(**(base | overrides))


class FakeActivationSession:
    """Durable-store double: ONE conditional transaction per activation.

    ``calls`` is the transaction log the single-commit shape is asserted
    against; ``commit_activation`` is conditional exactly the way the
    domain demands — if the world no longer matches the guards'
    expectations it applies NOTHING (consumption, switch and fence move
    together or not at all).
    """

    def __init__(self, current: ActivePlanState) -> None:
        self._state = current
        self.calls: list[tuple[str, ActivationRecord]] = []
        self.consumed: dict[str, ActivationRecord] = {}

    def snapshot(self) -> ActivePlanState:
        return self._state

    def prior_activation(self, decision_id: str) -> ActivationRecord | None:
        return self.consumed.get(decision_id)

    def commit_activation(self, record: ActivationRecord) -> None:
        self.calls.append(("commit_activation", record))
        state = self._state
        if (
            record.parent_revision != state.active_revision
            or record.authorization_epoch != state.authorization_epoch
            or record.work_contract_digest != state.work_contract_digest
        ):
            raise RuntimeError("conditional transaction lost the race — nothing applied")
        self.consumed[record.decision_id] = record
        self._state = replace(
            self._state,
            active_revision=record.activated_revision,
            publication_epoch=record.publication_epoch,
        )


def _question(**overrides) -> Question:
    base = {
        "question_id": "Q1",
        "work_id": "wp-demo-1",
        "reason": "Migration authority for the expiry dedup key is unclear.",
    }
    return Question(**(base | overrides))


class TestTacticalPolicy:
    def test_enabled_policy_parses_to_the_explicit_allowlist(self):
        policy: TacticalPolicy = parse_tactical_policy(
            _contract(tactical_revision_policy="reorder,add_step")
        )
        assert policy.status == "enabled"
        assert policy.permits({"reorder"}) is True
        assert policy.permits({"reorder", "add_step"}) is True
        assert policy.permits({"remove_step"}) is False  # containment, not membership

    @pytest.mark.parametrize(
        ("raw", "status"),
        [
            ("disabled", "disabled"),
            ("", "unset"),  # silence is not consent
            ("reorder,sideways", "unknown"),  # a typo narrows, never widens
        ],
    )
    def test_non_enabled_statuses_permit_nothing(self, raw, status):
        policy = parse_tactical_policy(_contract(tactical_revision_policy=raw))
        assert policy.status == status
        assert policy.permits(frozenset()) is False
        assert policy.permits({"reorder"}) is False


class TestTransformationKinds:
    def test_every_delta_shape_gets_its_named_kind(self):
        old = _revision(
            [
                *_base_steps(),
                _step("S9", "Write the operator runbook note.", acceptance_refs=[]),
            ]
        )
        new = _revision(
            [
                # reordered survivors + a reworded S1 + a genuinely new S3,
                # while the old runbook step disappears
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step("S1", "Inspect idempotency behavior and record digests."),
                _step("S3", "Add the operator runbook note.", acceptance_refs=[]),
            ],
            revision=2,
            summary="Updated summary.",
        )
        kinds = transformation_kinds(old, new)
        assert kinds == {"reorder", "edit_step", "add_step", "remove_step", "reword"}

    def test_an_identical_plan_has_no_transformation_kinds(self):
        old = _revision(_base_steps())
        assert transformation_kinds(old, _revision(_base_steps())) == frozenset()


class TestClassifyRevision:
    def test_reordering_and_internal_edits_stay_tactical(self):
        contract = _contract()
        old = _revision(_base_steps())
        new = _revision(
            [
                # reordered: the implementing step is listed first
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step("S1", "Inspect idempotency behavior and record digests."),
                _step("S3", "Write the operator runbook note."),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "tactical_internal"

    def test_new_write_repo_is_material_scope(self):
        contract = _contract()
        old = _revision(_base_steps())
        new = _revision(
            [
                *_base_steps(),
                _step(
                    "S3",
                    "Mirror the fix into Billing.",
                    write_repository_id="billing",
                    depends_on=["S2"],
                ),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "material_scope"

    def test_old_plan_write_repo_grants_nothing(self):
        """The NXT-20 characterization: the old plan is never authority.

        The OLD plan writes ``billing`` — authorized once by a
        historically broader contract — while the CURRENT contract's
        write scope names only ``orders``. Under the pre-fix classifier
        the old plan's write repositories flowed into the authorized set
        and this revision passed as tactical; it must be material.
        """
        contract = _contract()
        old = _revision(
            [
                *_base_steps(),
                _step(
                    "S3",
                    "Mirror the fix into Billing.",
                    write_repository_id="billing",
                    depends_on=["S2"],
                ),
            ]
        )
        new = _revision(
            [
                # S3 carried over UNCHANGED — the old plan's grant is the
                # only thing that could authorize it, and it must not
                _step("S1", "Inspect existing idempotency behavior."),
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step(
                    "S3",
                    "Mirror the fix into Billing.",
                    write_repository_id="billing",
                    depends_on=["S2"],
                ),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "material_scope"

    def test_dropped_acceptance_ref_is_material_contract(self):
        contract = _contract()
        old = _revision(_base_steps())
        new = _revision(
            [
                _step("S1", "Inspect existing idempotency behavior.", acceptance_refs=[]),
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                    acceptance_refs=[],
                ),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "material_contract"

    @pytest.mark.parametrize("impact", [["schema"], ["migration"], ["Schema"], ["db", "MIGRATION"]])
    def test_schema_or_migration_impact_is_material_migration(self, impact):
        contract = _contract()
        old = _revision(_base_steps())
        new = _revision(
            [
                *_base_steps(),
                _step("S3", "Add the dedup key table.", impact=impact, depends_on=["S2"]),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "material_migration"

    @pytest.mark.parametrize("impact", [["api"], ["public-api"], ["event"], ["effect"]])
    def test_declared_api_or_event_impact_is_material_migration(self, impact):
        """New API/schema/event/effect classes are material BY DECLARATION."""
        contract = _contract()
        old = _revision(_base_steps())
        new = _revision(
            [
                *_base_steps(),
                _step("S3", "Expose the expiry hook.", impact=impact, depends_on=["S2"]),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "material_migration"

    def test_carried_over_schema_step_does_not_poison_tactical_edits(self):
        """A step's material class was paid for when it was approved.

        Carrying an approved schema step forward byte-for-byte must not
        make every later tactical revision material — the DELTA is what
        is classified (a genuine refactor succeeds without unnecessary
        reapproval).
        """
        contract = _contract()
        schema_step = _step("S5", "Add the dedup key table.", impact=["schema"], depends_on=["S2"])
        old = _revision([*_base_steps(), schema_step])
        new = _revision(
            [
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step("S1", "Inspect existing idempotency behavior."),
                schema_step,  # unchanged, still last
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "tactical_internal"

    @pytest.mark.parametrize(
        "impact",
        [[], ["internal", "quantum-entangle"], ["public-contract"]],
    )
    def test_unknown_or_missing_impact_is_decision_required_never_tactical(self, impact):
        """Fail closed: omitted and unrecognized impact classes go to a human.

        A change must never pass solely because an impact tag was
        omitted — and a class outside the known tactical vocabulary is
        unknown materiality, not a quiet ``tactical_internal``.
        """
        contract = _contract()
        old = _revision(_base_steps())
        new = _revision(
            [
                *_base_steps(),
                _step("S3", "Do the new thing.", impact=impact, depends_on=["S2"]),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == "decision_required"

    @pytest.mark.parametrize(
        ("policy", "expected"),
        [
            ("disabled", "decision_required"),
            ("", "decision_required"),
            ("reorder,sideways", "decision_required"),
        ],
    )
    def test_disabled_unset_or_unknown_policy_permits_no_automatic_revision(self, policy, expected):
        contract = _contract(tactical_revision_policy=policy)
        old = _revision(_base_steps())
        new = _revision(  # a pure reorder — the most benign delta there is
            [
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step("S1", "Inspect existing idempotency behavior."),
            ],
            revision=2,
        )
        assert classify_revision(old, new, contract) == expected

    def test_transformation_outside_the_policy_is_decision_required(self):
        contract = _contract(tactical_revision_policy="reorder")
        old = _revision(
            [
                *_base_steps(),
                _step("S9", "Write the operator runbook note.", acceptance_refs=[]),
            ]
        )
        added = _revision(
            [
                *_base_steps(),
                _step("S9", "Write the operator runbook note.", acceptance_refs=[]),
                _step("S3", "Add the operator checklist.", acceptance_refs=[]),
            ],
            revision=2,
        )
        removed = _revision(_base_steps(), revision=2)  # S9 dropped
        assert classify_revision(old, added, contract) == "decision_required"  # add_step
        assert classify_revision(old, removed, contract) == "decision_required"  # remove_step

    def test_same_acceptance_ids_with_rewritten_write_step_need_policy(self):
        """Preserved acceptance IDs do not prove semantic non-materiality.

        The step keeps referencing AC-1, but its objective — what code
        the plan changes — is rewritten. That is an ``edit_step``
        transformation: it lands automatically ONLY when the contract
        explicitly pre-approved editing steps, never merely because the
        acceptance IDs survived.
        """
        old = _revision(_base_steps())
        rewritten = _revision(
            [
                _step("S1", "Inspect existing idempotency behavior."),
                _step(
                    "S2",
                    "Rewrite the Orders service onto the new event bus entirely.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
            ],
            revision=2,
        )
        narrow = _contract(tactical_revision_policy="reorder,add_step")
        assert classify_revision(old, rewritten, narrow) == "decision_required"
        granting = _contract(tactical_revision_policy="reorder,edit_step")
        assert classify_revision(old, rewritten, granting) == "tactical_internal"


class TestApplyTactical:
    def test_tactical_applied_bumps_revision_and_emits_event(self):
        contract = _contract()
        old = _revision(_base_steps())  # revision 1
        new = _revision(
            [
                _step("S1", "Inspect idempotency behavior and record digests."),
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step("S3", "Write the operator runbook note."),
            ],
            revision=1,
        )
        applied, event = apply_tactical(old, new, contract)

        assert (applied.revision, applied.parent_revision) == (2, 1)
        assert applied.preserved_step_ids == ["S1", "S2"]  # survivors keep their WIP anchor
        assert event["schema"] == "forge.revision.event/1"
        assert event["kind"] == "tactical_applied"
        assert event["revision"] == 2
        assert event["parent"] == 1
        assert event["change_log"] == [
            "changed S1: Inspect existing idempotency behavior. "
            "-> Inspect idempotency behavior and record digests.",
            "added S3: Write the operator runbook note.",
        ]
        # the event names the exact transformations and policy it landed under
        assert event["transformation_kinds"] == ["add_step", "edit_step"]
        assert event["tactical_policy"] == POLICY_ALL

    def test_policy_allowed_transform_lands_under_a_narrow_policy(self):
        contract = _contract(tactical_revision_policy="reorder")
        old = _revision(_base_steps())
        reordered = _revision(  # same steps, swapped order — a pure reorder
            [
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step("S1", "Inspect existing idempotency behavior."),
            ],
            revision=2,
        )
        applied, event = apply_tactical(old, reordered, contract)
        assert (applied.revision, applied.parent_revision) == (2, 1)
        assert event["transformation_kinds"] == ["reorder"]
        assert event["tactical_policy"] == "reorder"

    def test_material_refuses_without_an_approval_path(self):
        contract = _contract()
        old = _revision(_base_steps())
        material = _revision(
            [
                *_base_steps(),
                _step(
                    "S3",
                    "Mirror the fix into Billing.",
                    write_repository_id="billing",
                    depends_on=["S2"],
                ),
            ],
            revision=2,
        )
        with pytest.raises(ValueError, match="cannot land as a tactical edit"):
            apply_tactical(old, material, contract)

    def test_decision_required_refuses_even_though_not_material(self):
        contract = _contract()
        old = _revision(_base_steps())
        unknown = _revision(
            [*_base_steps(), _step("S3", "Do the new thing.", impact=[])],
            revision=2,
        )
        with pytest.raises(ValueError, match="decision_required"):
            apply_tactical(old, unknown, contract)

    def test_disabled_policy_permits_no_automatic_revision(self):
        """apply_tactical reads the policy itself (NXT-20), not the caller."""
        contract = _contract(tactical_revision_policy="disabled")
        old = _revision(_base_steps())
        reordered = _revision(
            [
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
                _step("S1", "Inspect existing idempotency behavior."),
            ],
            revision=2,
        )
        # classify already routes a disabled policy to decision_required,
        # and apply_tactical re-derives the same verdict from the policy
        # itself — neither trusts the caller's classification
        with pytest.raises(ValueError, match="decision_required"):
            apply_tactical(old, reordered, contract)
        assert parse_tactical_policy(contract).permits({"reorder"}) is False


class TestChangeLog:
    def test_added_removed_and_changed_objectives_each_get_a_line(self):
        old = _revision(_base_steps())
        new = _revision(
            [
                _step("S1", "Inspect behavior and record digests."),
                _step("S3", "Write the operator runbook note."),
            ],
            revision=2,
        )
        assert change_log(old, new) == [
            "changed S1: Inspect existing idempotency behavior. "
            "-> Inspect behavior and record digests.",
            "removed S2: Implement the authorized Orders change.",
            "added S3: Write the operator runbook note.",
        ]


class TestRevisionDecision:
    def test_stale_epoch_refuses_both_verdicts(self):
        decision = _decision()
        with pytest.raises(ValueError, match="stale authorization epoch"):
            decision.approve("approver", decision.authorization_epoch + 1)
        with pytest.raises(ValueError, match="stale authorization epoch"):
            decision.reject("approver", decision.authorization_epoch - 1, "too wide")
        assert decision.decided is False  # the fence never records anything

    def test_deciding_twice_refuses(self):
        approved = _decision().approve("approver", 3)
        with pytest.raises(ValueError, match="already approved"):
            approved.approve("other", 3)
        with pytest.raises(ValueError, match="already approved"):
            approved.reject("other", 3, "reconsidered")

    def test_expire_closes_the_pending_slot_terminally(self):
        expired = _decision().expire()
        assert (expired.decided, expired.decision) == (True, "expired")
        with pytest.raises(ValueError, match="already expired"):
            expired.approve("approver", 3)


class TestActivateRevision:
    """NXT-19: activation binds the decision to the exact proposal and work.

    The review's characterization: pre-fix, ``activate_revision`` checked
    only "approved + newer number" — a decision for work-A/plan-A could
    activate work-B/plan-B. Every test below drives a mismatch through
    the full guard and asserts the typed refusal leaves the session's
    call log EMPTY (nothing consumed, nothing switched, no fence bump).
    """

    def test_approved_decision_activates_in_one_conditional_transaction(self):
        proposed = _revision(_base_steps(), revision=2)
        decision = _approved(proposed)
        current = _current()
        session: ActivationSession = FakeActivationSession(current)

        activated = activate_revision(decision, proposed, current, session)

        assert (activated.revision, activated.parent_revision) == (2, 1)
        # the single conditional transaction: ONE commit carrying
        # consumption + the active-revision switch + the fence bump —
        # a crash between them is unrepresentable
        assert [name for name, _ in session.calls] == ["commit_activation"]
        record = session.calls[0][1]
        assert record.decision_id == decision.decision_id  # the decision is consumed...
        assert record.activated_revision == 2  # ...the active revision switches...
        assert record.publication_epoch == current.publication_epoch + 1  # ...fence bumps
        assert record.activated_plan_digest == plan_digest(proposed)
        assert set(session.consumed) == {decision.decision_id}
        after = session.snapshot()
        assert (after.active_revision, after.publication_epoch) == (
            2,
            current.publication_epoch + 1,
        )

    def test_replay_returns_the_prior_outcome_without_new_effects(self):
        proposed = _revision(_base_steps(), revision=2)
        decision = _approved(proposed)
        session = FakeActivationSession(_current())
        first = activate_revision(decision, proposed, _current(), session)

        # the same approval arrives again — against the world as it now is
        replayed = activate_revision(decision, proposed, session.snapshot(), session)

        assert replayed == first
        assert len(session.calls) == 1  # no second transaction

    def test_consumed_decision_cannot_activate_different_content(self):
        proposed = _revision(_base_steps(), revision=2)
        decision = _approved(proposed)
        session = FakeActivationSession(_current())
        activate_revision(decision, proposed, _current(), session)

        different = _revision(
            [*_base_steps(), _step("S3", "Sneak an extra step in.", acceptance_refs=[])],
            revision=2,
        )
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(decision, different, session.snapshot(), session)
        assert excinfo.value.code == "decision_already_consumed"
        assert len(session.calls) == 1  # the refusal consumed nothing new

    def test_decision_for_another_work_cannot_activate(self):
        proposed = _revision(_base_steps(), revision=2)  # work wp-demo-1
        foreign_decision = _approved(proposed, work_id="wp-a")  # minted for work A
        session = FakeActivationSession(_current())
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(foreign_decision, proposed, _current(), session)
        assert excinfo.value.code == "work_mismatch"
        assert session.calls == []
        assert session.consumed == {}

    def test_work_a_decision_cannot_activate_in_work_bs_world(self):
        """The review's cross-work regression: A/plan-A vs B/plan-B refused."""
        proposed = _revision(_base_steps(), revision=2, work_id="wp-a", plan_id="plan-a")
        decision = _approved(proposed)  # decision for work A / plan A
        current = _current(work_id="wp-b", plan_id="plan-b")  # the world is work B
        session = FakeActivationSession(current)
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(decision, proposed, current, session)
        assert excinfo.value.code == "current_work_mismatch"
        assert session.calls == []  # nothing consumed
        assert session.snapshot().active_revision == 1  # nothing switched

    def test_cross_plan_and_identity_mismatches_refuse(self):
        proposed = _revision(_base_steps(), revision=2)
        session = FakeActivationSession(_current())
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(
                _approved(proposed), proposed, _current(plan_id="plan-other"), session
            )
        assert excinfo.value.code == "plan_mismatch"

        wrong_identity = _approved(proposed, proposed_revision_id="plan-demo-1#9")
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(wrong_identity, proposed, _current(), session)
        assert excinfo.value.code == "proposal_identity_mismatch"
        assert session.calls == []  # still nothing consumed across both attempts

    def test_changed_proposal_body_with_same_revision_number_refuses(self):
        proposed = _revision(_base_steps(), revision=2)
        decision = _approved(proposed)
        mutated = _revision(
            [
                _step("S1", "Quietly rewritten objective."),
                _step(
                    "S2",
                    "Implement the authorized Orders change.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
            ],
            revision=2,  # same revision number, different content
        )
        session = FakeActivationSession(_current())
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(decision, mutated, _current(), session)
        assert excinfo.value.code == "proposed_digest_mismatch"
        assert session.calls == []
        assert session.consumed == {}

    def test_contract_digest_mismatches_refuse(self):
        proposed = _revision(_base_steps(), revision=2)
        session = FakeActivationSession(_current())
        judged_elsewhere = _approved(proposed, work_contract_digest=D_STALE)
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(judged_elsewhere, proposed, _current(), session)
        assert excinfo.value.code == "contract_digest_mismatch"

        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(
                _approved(proposed), proposed, _current(work_contract_digest=D_STALE), session
            )
        assert excinfo.value.code == "current_contract_mismatch"
        assert session.calls == []

    def test_approval_after_parent_advancement_fails_without_overwriting(self):
        proposed = _revision(_base_steps(), revision=2)
        decision = _approved(proposed)  # expects parent revision 1...
        moved_on = _current(active_revision=3)  # ...but revision 3 is active
        session = FakeActivationSession(moved_on)
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(decision, proposed, moved_on, session)
        assert excinfo.value.code == "parent_mismatch"
        assert session.calls == []
        assert session.snapshot().active_revision == 3  # newer work intact

    def test_non_following_revision_refuses(self):
        same_number = _revision(_base_steps(), revision=1)
        decision = _approved(same_number, parent_revision=1)
        session = FakeActivationSession(_current())
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(decision, same_number, _current(), session)
        assert excinfo.value.code == "revision_not_forward"
        assert session.calls == []

    def test_stale_authorization_epoch_refuses(self):
        proposed = _revision(_base_steps(), revision=2)
        decision = _approved(proposed)  # granted in epoch 3
        newer_epoch = _current(authorization_epoch=4)  # the world moved
        session = FakeActivationSession(newer_epoch)
        with pytest.raises(ActivationRefused) as excinfo:
            activate_revision(decision, proposed, newer_epoch, session)
        assert excinfo.value.code == "stale_authorization_epoch"
        assert session.calls == []
        assert session.consumed == {}

    def test_rejected_expired_and_undecided_never_activate(self):
        proposed = _revision(_base_steps(), revision=2)
        session = FakeActivationSession(_current())
        rejected = _decision(proposed).reject("approver", 3, "scope widened")
        with pytest.raises(ActivationRefused, match="rejected") as excinfo:
            activate_revision(rejected, proposed, _current(), session)
        assert excinfo.value.code == "not_approved"
        with pytest.raises(ActivationRefused, match="expired"):
            activate_revision(_decision(proposed).expire(), proposed, _current(), session)
        with pytest.raises(ActivationRefused, match="undecided"):
            activate_revision(_decision(proposed), proposed, _current(), session)
        assert session.calls == []  # none of the three consumed anything

    def test_proposed_revision_identity_format(self):
        proposed = _revision(_base_steps(), revision=2)
        assert proposed_revision_identity(proposed) == "plan-demo-1#2"


class TestQuestion:
    def test_required_scope_refuses_outsiders(self):
        question = _question(required_actor_scope=("customer",))
        with pytest.raises(ValueError, match="may not answer"):
            question.answer_question("yes, migrate", "helpful-subagent", "subagent")
        answered = question.answer_question("yes, migrate", "customer-1", "customer")
        assert (answered.answered, answered.answer, answered.answered_by) == (
            True,
            "yes, migrate",
            "customer-1",
        )

    def test_open_scope_accepts_any_actor(self):
        answered = _question().answer_question("42", "anyone", "operator")
        assert answered.answered is True

    def test_closed_options_refuse_free_text(self):
        question = _question(options=("A", "B"), free_text_allowed=False)
        with pytest.raises(ValueError, match="closed options"):
            question.answer_question("C", "customer-1", "customer")
        assert question.answer_question("A", "customer-1", "customer").answer == "A"
        free_form = _question(options=("A", "B"))  # free text still allowed by default
        assert free_form.answer_question("A and a caveat", "customer-1", "customer").answered

    def test_questions_answer_exactly_once_and_never_default(self):
        answered = _question().answer_question("yes", "customer-1", "customer")
        with pytest.raises(ValueError, match="already answered"):
            answered.answer_question("no actually", "customer-1", "customer")
        with pytest.raises(ValueError, match="never defaulted"):
            _question().answer_question("   ", "customer-1", "customer")

    @pytest.mark.parametrize(
        ("now", "expected"),
        [
            ("2026-09-20T23:59:59Z", False),
            ("2026-09-21T00:00:00Z", True),  # the deadline itself is past
            ("2026-09-22T00:00:00Z", True),
        ],
    )
    def test_expiry_by_iso_deadline(self, now, expected):
        question = _question(expires_at="2026-09-21T00:00:00Z")
        assert question.is_expired(now) is expected

    def test_questions_without_a_deadline_never_expire(self):
        assert _question().is_expired("2099-01-01T00:00:00Z") is False


class TestRouteQuestion:
    def test_questions_route_to_the_parent_never_fanned_out(self):
        question = _question()
        assert route_question(question, ["spec-writer", "test-runner", "reviewer"]) == "parent"


class TestInvalidationSet:
    def test_step_invalidation_and_digest_mismatch_mark_superseded_not_deleted(self):
        old = _revision(_base_steps())
        new = _revision(
            [
                _step("S1", "Inspect existing idempotency behavior."),
                _step(
                    "S2",
                    "Rework the change after the blocking answer.",
                    write_repository_id="orders",
                    depends_on=["S1"],
                ),
            ],
            revision=2,
            parent_revision=1,
            invalidated_step_ids=["S2"],
        )
        active_digest = plan_digest(new)

        def binding(step_id: str, plan_digest_value: str) -> dict:
            return {
                "step_id": step_id,
                "input_digest": "a" * 64,
                "source_digest": "b" * 64,
                "plan_digest": plan_digest_value,
                "environment_digest": "c" * 64,
            }

        result = invalidation_set(
            old,
            new,
            {
                "ev-1": binding("S1", active_digest),
                "ev-2": binding("S2", active_digest),
                "ev-3": binding("S1", D_STALE),
            },
        )
        assert result["preserved"] == ["ev-1"]
        assert [evidence_id for evidence_id, _ in result["invalidated"]] == ["ev-2", "ev-3"]
        assert "step S2 invalidated" in result["invalidated"][0][1]
        assert "plan digest mismatch" in result["invalidated"][1][1]
        # superseded == the invalidated ids with their reasons; nothing deleted
        assert result["superseded"] == dict(result["invalidated"])
        assert set(result["superseded"]) == {"ev-2", "ev-3"}


class TestStaleCallbackGuard:
    def test_only_the_active_plans_callbacks_advance(self):
        assert stale_callback_guard(D_CONTRACT, D_CONTRACT) is True
        assert stale_callback_guard(D_STALE, D_CONTRACT) is False
        assert stale_callback_guard("", D_CONTRACT) is False


class TestDecisionRecord:
    def test_record_shape_is_durable_and_reasoning_free(self):
        record = decision_record(
            "d-1",
            "Q1",
            ["reuse the events table", "add a dedup key table"],
            "add a dedup key table",
            ["ev-1", "ev-2"],
            ["Billing stays a separate scope approval."],
            "customer-1",
        )
        assert record == {
            "schema": "forge.decision.record/1",
            "decision_id": "d-1",
            "question_id": "Q1",
            "alternatives": ["reuse the events table", "add a dedup key table"],
            "chosen": "add a dedup key table",
            "evidence_ids": ["ev-1", "ev-2"],
            "assumptions": ["Billing stays a separate scope approval."],
            "approver": "customer-1",
        }

    @pytest.mark.parametrize(
        ("chosen", "alternatives"),
        [
            ("use sk-live-key-1", ["safe"]),
            ("safe", ["safe", "PRIVATE chain of thought"]),
        ],
    )
    def test_secret_looking_values_refuse(self, chosen, alternatives):
        with pytest.raises(ValueError, match="no secrets"):
            decision_record("d-1", "Q1", alternatives, chosen, [], [], "customer-1")


class TestFreshSessionBrief:
    def test_brief_assembles_from_durable_artifacts(self):
        contract = _contract()
        revision = _revision(_base_steps(), revision=2, parent_revision=1)
        decisions = [
            {"chosen": "add a dedup key table", "alternatives": ["reuse the events table"]},
            {"chosen": "idempotent consumer", "alternatives": ["at-least-once producer"]},
        ]
        brief = fresh_session_brief(contract, revision, decisions, "ck-9 partial at S2")
        assert brief == {
            "schema": "forge.session.brief/1",
            "objective": contract.objective,
            "write_scope": [{"repository_id": "orders", "paths": ["src/**", "tests/**"]}],
            "plan_summary": revision.summary,
            "step_objectives": {
                "S1": "Inspect existing idempotency behavior.",
                "S2": "Implement the authorized Orders change.",
            },
            "decisions": ["add a dedup key table", "idempotent consumer"],
            "checkpoint": "ck-9 partial at S2",
        }

    def test_brief_carries_a_null_checkpoint_when_there_is_none(self):
        brief = fresh_session_brief(_contract(), _revision(_base_steps()), [], None)
        assert brief["checkpoint"] is None
        assert brief["decisions"] == []


# ---------------------------------------------------------------------------
# R28-18: the durable activation transaction + the /approve-revision ingress
# ---------------------------------------------------------------------------


class _DurableWorld:
    """A sqlite-backed FlowRun evidence store for the activation transaction."""

    def __init__(self, active: ActivePlanState) -> None:
        self._initial = active
        self.factory = None
        self.run_id = "a" * 32

    async def start(self) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from forge.durable import FlowRun
        from forge.models.base import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        async with self.factory() as session:
            session.add(FlowRun(id=self.run_id, project_id=1, status="planning"))
            await session.commit()
        await self.set_active(self._initial)

    async def set_active(self, active: ActivePlanState) -> None:
        from forge.adaptive.revisions import ACTIVE_PLAN_KEY
        from forge.durable import FlowRun

        async with self.factory() as session:
            run = await session.get(FlowRun, self.run_id)
            merged = dict(run.evidence or {})
            merged[ACTIVE_PLAN_KEY] = {
                "work_id": active.work_id,
                "plan_id": active.plan_id,
                "active_revision": active.active_revision,
                "work_contract_digest": active.work_contract_digest,
                "authorization_epoch": active.authorization_epoch,
                "publication_epoch": active.publication_epoch,
            }
            run.evidence = merged
            await session.commit()

    async def evidence(self) -> dict:
        from forge.durable import FlowRun

        async with self.factory() as session:
            run = await session.get(FlowRun, self.run_id)
            return dict(run.evidence or {})

    async def outbox_events(self) -> list[tuple[str, dict]]:
        from sqlalchemy import select

        from forge.durable import Outbox

        async with self.factory() as session:
            rows = (await session.execute(select(Outbox))).scalars().all()
        return [(row.event_type, dict(row.payload)) for row in rows]


@pytest.fixture()
async def durable(tmp_path):
    world = _DurableWorld(_current())
    await world.start()
    return world


class TestDurableActivation:
    async def test_activation_is_one_durable_transaction(self, durable):
        from forge.adaptive.revisions import (
            ACTIVE_PLAN_KEY,
            PENDING_PROPOSAL_KEY,
            REVISION_ACTIVATIONS_KEY,
            activate_pending_revision,
            plan_digest,
            read_active_plan,
            stage_pending_revision,
        )

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )
        staged_events = [
            e for e in await durable.outbox_events() if e[0] == "revision.proposal_staged"
        ]
        assert staged_events, "staging journaled its outbox row"

        outcome = await activate_pending_revision(
            durable.factory, durable.run_id, decision.decision_id, decided_by="alice"
        )
        assert outcome.status == "activated"
        assert outcome.revision is not None and outcome.revision.revision == 2
        evidence = await durable.evidence()
        # 1. RECORD: the journal carries the activation keyed by decision id.
        journal = evidence[REVISION_ACTIVATIONS_KEY]
        assert decision.decision_id in journal
        # 2. SWITCH: the durable active-plan pointer moved, with the digest
        #    a late callback must be fenced against and the bumped epoch.
        active = evidence[ACTIVE_PLAN_KEY]
        assert active["active_revision"] == 2
        assert active["plan_digest"] == plan_digest(proposed)
        assert active["publication_epoch"] == _current().publication_epoch + 1
        assert active["activated_by_decision"] == decision.decision_id
        # 3. The pending slot is CONSUMED, and the dispatch leg reads the
        #    durable record — not an in-memory function.
        assert PENDING_PROPOSAL_KEY not in evidence
        assert (await read_active_plan(durable.factory, durable.run_id)) == active
        assert any(e[0] == "revision.activated" for e in await durable.outbox_events())
        # A late OLD-revision callback cannot advance the new revision.
        assert stale_callback_guard("0" * 64, active["plan_digest"]) is False
        assert stale_callback_guard(plan_digest(proposed), active["plan_digest"]) is True

    async def test_two_deliveries_activate_one_revision_and_one_continuation(self, durable):
        from forge.adaptive.revisions import (
            REVISION_ACTIVATIONS_KEY,
            activate_pending_revision,
            stage_pending_revision,
        )

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )
        first = await activate_pending_revision(
            durable.factory, durable.run_id, decision.decision_id, decided_by="alice"
        )
        # A SECOND, DIFFERENT delivery of the same decision: idempotent.
        second = await activate_pending_revision(
            durable.factory, durable.run_id, decision.decision_id, decided_by="bob"
        )
        assert second.status == "already_active"
        assert second.record == first.record
        evidence = await durable.evidence()
        assert list(evidence[REVISION_ACTIVATIONS_KEY]) == [decision.decision_id]
        activations = [e for e in await durable.outbox_events() if e[0] == "revision.activated"]
        assert len(activations) == 1  # one continuation, one switch

    async def test_the_guard_runs_against_durable_state_not_the_stash(self, durable):
        """A parent revision that moved BETWEEN staging and approval refuses —
        the CAS compares the decision's expectations against the durable
        active-plan pointer, never against the state the proposal came with."""
        from forge.adaptive.revisions import (
            PENDING_PROPOSAL_KEY,
            activate_pending_revision,
            stage_pending_revision,
        )

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )
        # The world moved: revision 3 activated by someone else meanwhile.
        await durable.set_active(_current(active_revision=3, publication_epoch=9))
        outcome = await activate_pending_revision(
            durable.factory, durable.run_id, decision.decision_id, decided_by="alice"
        )
        assert outcome.status == "refused"
        assert outcome.code == "parent_mismatch"
        evidence = await durable.evidence()
        assert PENDING_PROPOSAL_KEY in evidence  # the decision was NOT consumed
        assert evidence["active_plan"]["active_revision"] == 3  # nothing switched

    async def test_wrong_and_missing_decisions_refuse_without_effects(self, durable):
        from forge.adaptive.revisions import activate_pending_revision, stage_pending_revision

        # No pending proposal at all.
        outcome = await activate_pending_revision(
            durable.factory, durable.run_id, "rd-none", decided_by="alice"
        )
        assert (outcome.status, outcome.code) == ("refused", "no_pending_proposal")

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )
        # A DIFFERENT decision id than the staged one.
        outcome = await activate_pending_revision(
            durable.factory, durable.run_id, "rd-other", decided_by="alice"
        )
        assert (outcome.status, outcome.code) == ("refused", "stale_decision")
        assert "revision.activated" not in [e[0] for e in await durable.outbox_events()]

    async def test_stale_authorization_epoch_refuses(self, durable):
        from forge.adaptive.revisions import activate_pending_revision, stage_pending_revision

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)  # epoch 3
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )
        await durable.set_active(_current(authorization_epoch=4))  # the epoch bumped
        outcome = await activate_pending_revision(
            durable.factory, durable.run_id, decision.decision_id, decided_by="alice"
        )
        assert outcome.status == "refused"
        assert outcome.code in ("approval_refused", "stale_authorization_epoch")

    async def test_changed_proposal_body_under_a_consumed_decision_refuses(self, durable):
        """The domain door's replay rule: once a decision is consumed, a
        DIFFERENT proposal body under the same decision id refuses — the
        idempotency is content-bound, not id-blind."""
        from forge.adaptive.revisions import ActivationRefused, stage_pending_revision

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )

        session = FakeActivationSession(_current())
        activate_revision(_approved(proposed), proposed, _current(), session)

        tampered = _revision(
            [_step("S9", "Sneak an unauthorized write.", write_repository_id="billing")],
            revision=2,
            parent_revision=1,
        )
        with pytest.raises(ActivationRefused, match="decision_already_consumed"):
            activate_revision(
                _approved(tampered),
                tampered,
                _current(active_revision=1),
                session,
            )


class TestActivationMovesTheDurablePlanIdentity:
    """R32-11 — the activation transaction also moves the surfaces the
    production dispatch reads: the run row's ``plan_digest`` (the claim
    the /go gate validates and the dispatch boundary compares) and, when
    the run carries a human gate, a FRESH approval generation bound to
    the revised digest — the /approve-revision WAS the human approval of
    the new content, so the next /go consumes a decision bound to the
    plan it will actually run."""

    async def test_activation_switches_the_run_rows_plan_digest(self, durable):
        from forge.adaptive.revisions import activate_pending_revision, stage_pending_revision
        from forge.durable import FlowRun

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )
        async with durable.factory() as session:
            run = await session.get(FlowRun, durable.run_id)
            run.plan_digest = "a" * 64  # the published plan the gate bound
            await session.commit()

        outcome = await activate_pending_revision(
            durable.factory, durable.run_id, decision.decision_id, decided_by="alice"
        )
        assert outcome.status == "activated"
        async with durable.factory() as session:
            run = await session.get(FlowRun, durable.run_id)
            assert run.plan_digest == plan_digest(proposed)  # the ACTIVE plan

    async def test_activation_rebinds_the_human_gate_to_the_revised_digest(self, tmp_path):
        from forge.adaptive.revisions import activate_pending_revision, stage_pending_revision

        world = _DurableWorld(_current())
        await world.start()
        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(world.factory, world.run_id, decision, proposed, _current())
        # The run carries a published plan's gate (generation 0, the old
        # digest, an open window and its policy/spec bindings).
        from datetime import datetime, timedelta, timezone

        from forge.durable import GateApproval

        async with world.factory() as session:
            session.add(
                GateApproval(
                    flow_run_id=world.run_id,
                    generation=0,
                    plan_digest="a" * 64,
                    base_sha="1" * 40,
                    policy_digest="2" * 64,
                    approver_user_id=0,
                    source_event_id="plan_publication",
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                )
            )
            await session.commit()

        outcome = await activate_pending_revision(
            world.factory, world.run_id, decision.decision_id, decided_by="alice"
        )
        assert outcome.status == "activated"
        from sqlalchemy import select

        async with world.factory() as session:
            gates = (
                (await session.execute(select(GateApproval).order_by(GateApproval.id)))
                .scalars()
                .all()
            )
        assert [gate.generation for gate in gates] == [0, 1]
        rebound = gates[1]
        assert rebound.plan_digest == plan_digest(proposed)
        assert rebound.consumed_at is None  # awaiting the /go that consumes it
        assert rebound.base_sha == "1" * 40  # the window/policy bindings survive
        assert rebound.policy_digest == "2" * 64
        assert rebound.source_event_id.startswith("approve-revision:")
        assert decision.decision_id in rebound.source_event_id


class TestApproveRevisionIngress:
    """/approve-revision through the REAL command router → the durable
    transaction → one journaled operator reply (R28-18)."""

    @staticmethod
    def _note(text: str, *, note_id: int, verb: str = "approve-revision") -> dict:
        return {
            "command": "adaptive_control",
            "provider": "gitlab",
            "adaptive_verb": verb,
            "project_id": 42,
            "issue_iid": 7,
            "author_username": "alice",
            "note_text": text,
            "note_id": note_id,
        }

    async def test_router_routes_approval_into_the_durable_transaction(self, tmp_path):
        from forge.adaptive.command_router import ControlCommandRouter
        from forge.adaptive.revisions import (
            PENDING_PROPOSAL_KEY,
            stage_pending_revision,
        )
        from forge.config import Settings
        from forge.durable import FlowRun
        from forge.models.base import Base
        from pydantic import SecretStr
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        run_id = "b" * 32
        async with factory() as session:
            session.add(
                FlowRun(id=run_id, project_id=42, issue_iid=7, provider="gitlab", status="planning")
            )
            await session.commit()

        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        # The run's durable active-plan state (what the guard checks against).
        from forge.adaptive.revisions import ACTIVE_PLAN_KEY

        async with factory() as session:
            run = await session.get(FlowRun, run_id)
            run.evidence = {
                ACTIVE_PLAN_KEY: {
                    "work_id": _current().work_id,
                    "plan_id": _current().plan_id,
                    "active_revision": 1,
                    "work_contract_digest": D_CONTRACT,
                    "authorization_epoch": 3,
                    "publication_epoch": 7,
                }
            }
            await session.commit()
        await stage_pending_revision(factory, run_id, decision, proposed, _current())

        posted: list[str] = []

        async def post(body: str) -> dict:
            posted.append(body)
            return {"id": len(posted)}

        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
            FORGE_BOT_USERNAME="forge-bot",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            FORGE_APPROVERS="alice",
        )
        router = ControlCommandRouter(session_factory=factory, settings=settings, post_note=post)

        result = await router.handle(
            self._note(f"/approve-revision {decision.decision_id}", note_id=5001)
        )
        assert result["status"] == "applied"
        assert result["verb"] == "approve-revision"
        assert result["run_id"] == run_id
        assert any("is now\nACTIVE" in body or "is now ACTIVE" in body for body in posted)
        async with factory() as session:
            run = await session.get(FlowRun, run_id)
            assert PENDING_PROPOSAL_KEY not in (run.evidence or {})
            assert (run.evidence or {})["active_plan"]["active_revision"] == 2

        # A second, different note approving the SAME decision: one reply,
        # no second switch (decision-id idempotency, not note-id dedup).
        result = await router.handle(
            self._note(f"/approve-revision {decision.decision_id}", note_id=5002)
        )
        assert result["status"] == "refused"  # nothing new applied
        assert any("already consumed" in body for body in posted)
        async with factory() as session:
            run = await session.get(FlowRun, run_id)
            assert (run.evidence or {})["active_plan"]["active_revision"] == 2

    async def test_non_approver_is_refused_with_a_note(self, tmp_path):
        from forge.adaptive.command_router import ControlCommandRouter
        from forge.config import Settings
        from forge.durable import FlowRun
        from forge.models.base import Base
        from pydantic import SecretStr
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                FlowRun(
                    id="c" * 32, project_id=42, issue_iid=7, provider="gitlab", status="planning"
                )
            )
            await session.commit()

        posted: list[str] = []

        async def post(body: str) -> dict:
            posted.append(body)
            return {"id": len(posted)}

        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
            FORGE_BOT_USERNAME="forge-bot",
            DATABASE_URL="sqlite+aiosqlite:///:memory:",
            FORGE_APPROVERS="alice",
        )
        router = ControlCommandRouter(session_factory=factory, settings=settings, post_note=post)
        result = await router.handle(
            self._note("/approve-revision rd-1", note_id=5003, verb="approve-revision")
            | {"author_username": "stranger"}
        )
        assert result["status"] == "refused"
        assert any("ignored" in body for body in posted)


class TestRevisionJourneyToDispatch:
    """NEXT-20 — from human decision to changed execution.

    /approve-revision activates the revised plan in one durable
    transaction; the NEXT /go then dispatches under the NEW active plan
    read from ``read_active_plan()`` (never the old plan comment): the
    brief envelope binds the revised plan's bytes, and a stale /go still
    carrying the superseded digest refuses. The revised plan comment
    notes "revised from <old-digest> to <new-digest>", rendered from the
    durable pointer the same transaction wrote."""

    async def _world_with_old_plan(self, tmp_path):
        """A durable world whose ACTIVE plan is revision 1 with a digest."""
        world = _DurableWorld(_current())
        await world.start()
        old_plan = _revision(_base_steps(), revision=1)
        async with world.factory() as session:
            from forge.adaptive.revisions import ACTIVE_PLAN_KEY
            from forge.durable import FlowRun

            run = await session.get(FlowRun, world.run_id)
            merged = dict(run.evidence or {})
            merged[ACTIVE_PLAN_KEY] = {
                **merged[ACTIVE_PLAN_KEY],
                "plan_digest": plan_digest(old_plan),
            }
            run.evidence = merged
            await session.commit()
        return world, old_plan

    async def test_approve_revision_then_the_next_go_dispatches_the_new_plan(self, tmp_path):
        from forge.adaptive.revisions import (
            activate_pending_revision,
            dispatch_plan_binding,
            stage_pending_revision,
        )

        world, old_plan = await self._world_with_old_plan(tmp_path)
        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(world.factory, world.run_id, decision, proposed, _current())
        outcome = await activate_pending_revision(
            world.factory, world.run_id, decision.decision_id, decided_by="alice"
        )
        assert outcome.status == "activated"

        # The NEXT /go: the dispatch reads the durable ACTIVE plan —
        # not the old plan comment — and binds to the revised digest.
        binding = await dispatch_plan_binding(world.factory, world.run_id)
        assert binding.dispatchable is True
        assert binding.plan_digest == plan_digest(proposed)
        assert binding.revised_from_digest == plan_digest(old_plan)
        assert binding.active_revision == 2

        # The brief envelope is built over the REVISED plan's bytes and
        # verifies; its identity is the digest the binding named.
        from forge.harnesses.brief_envelope import (
            build_brief_envelope,
            verify_brief_envelope,
        )

        envelope = build_brief_envelope(
            run_id=world.run_id,
            task_title="Add order reservation expiry",
            task_description="Idempotent expiry per AC-1.",
            plan_text=proposed.model_dump_json(),
            spec_digest="7" * 64,
        )
        verify_brief_envelope(  # does not raise: the envelope IS the new plan
            envelope["envelope_digest"],
            run_id=world.run_id,
            task_title="Add order reservation expiry",
            task_description="Idempotent expiry per AC-1.",
            plan_text=proposed.model_dump_json(),
            spec_digest="7" * 64,
        )
        # A late callback minted against the OLD plan cannot advance.
        assert stale_callback_guard(plan_digest(old_plan), binding.plan_digest) is False
        assert stale_callback_guard(binding.plan_digest, binding.plan_digest) is True

    async def test_the_revised_plan_comment_notes_old_to_new_digests(self, tmp_path):
        from forge.adaptive.revisions import (
            REVISION_NOTE_MARKER,
            activate_pending_revision,
            dispatch_plan_binding,
            plan_comment_revision_note,
            read_active_plan,
            stage_pending_revision,
        )

        world, old_plan = await self._world_with_old_plan(tmp_path)
        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(world.factory, world.run_id, decision, proposed, _current())
        await activate_pending_revision(
            world.factory, world.run_id, decision.decision_id, decided_by="alice"
        )

        # The comment footer renders from the durable pointer the
        # activation transaction wrote — it cannot drift from the record.
        document = await read_active_plan(world.factory, world.run_id)
        note = plan_comment_revision_note(
            document["revised_from_digest"],
            document["plan_digest"],
            decided_by="alice",
            decision_id=decision.decision_id,
        )
        old_digest, new_digest = plan_digest(old_plan), plan_digest(proposed)
        assert note == (
            f"{REVISION_NOTE_MARKER}\n"
            f"revised from {old_digest} to {new_digest}\n"
            f"approved by alice ({decision.decision_id})"
        )
        assert f"revised from {old_digest} to {new_digest}" in note

        # The digest pair the note names is exactly the dispatch binding.
        binding = await dispatch_plan_binding(world.factory, world.run_id)
        assert (binding.revised_from_digest, binding.plan_digest) == (old_digest, new_digest)

    async def test_a_stale_go_with_the_old_digest_is_refused(self, tmp_path):
        from forge.adaptive.revisions import (
            activate_pending_revision,
            dispatch_plan_binding,
            stage_pending_revision,
        )

        world, old_plan = await self._world_with_old_plan(tmp_path)
        proposed = _revision(_base_steps(), revision=2, parent_revision=1)
        decision = _decision(proposed)
        await stage_pending_revision(world.factory, world.run_id, decision, proposed, _current())
        await activate_pending_revision(
            world.factory, world.run_id, decision.decision_id, decided_by="alice"
        )

        # The /go that still carries the SUPERSESED plan's digest (read
        # off the old plan comment) refuses — nothing dispatches.
        stale = await dispatch_plan_binding(
            world.factory, world.run_id, claimed_plan_digest=plan_digest(old_plan)
        )
        assert stale.dispatchable is False
        assert stale.code == "stale_plan_digest"
        assert plan_digest(old_plan) in stale.reason
        assert plan_digest(proposed) in stale.reason

        # The corrected /go — claiming the digest the record names —
        # dispatches; and an unknown digest refuses as a mismatch.
        ok = await dispatch_plan_binding(
            world.factory, world.run_id, claimed_plan_digest=plan_digest(proposed)
        )
        assert ok.dispatchable is True
        unknown = await dispatch_plan_binding(
            world.factory, world.run_id, claimed_plan_digest="0" * 64
        )
        assert unknown.dispatchable is False
        assert unknown.code == "plan_digest_mismatch"

    async def test_a_run_without_an_active_plan_refuses_the_dispatch(self, tmp_path):
        from forge.adaptive.revisions import dispatch_plan_binding

        world = _DurableWorld(_current())
        await world.start()  # evidence active_plan exists here — so use a stranger
        stranger = "b" * 32
        async with world.factory() as session:
            from forge.durable import FlowRun

            session.add(FlowRun(id=stranger, project_id=1, status="planning"))
            await session.commit()

        binding = await dispatch_plan_binding(world.factory, stranger)

        assert binding.dispatchable is False
        assert binding.code == "no_active_plan"

    def test_the_note_marker_is_machine_extractable(self):
        from forge.adaptive.revisions import (
            REVISION_NOTE_MARKER,
            plan_comment_revision_note,
        )

        note = plan_comment_revision_note("a" * 64, "b" * 64)
        assert note.startswith(REVISION_NOTE_MARKER)
        assert f"revised from {'a' * 64} to {'b' * 64}" in note


# ----------------------------------------------------------------------
# Q39-02 (#321) — the ApprovedInput: the GitLab continuation's brief binds
# to the ACTIVE revision's TEXT. The activation persists the switched
# revision's CONTENT beside its digest; resolution re-verifies the digest,
# renders the brief from the content, and labels every honest fallback.
# ----------------------------------------------------------------------


def _pointer_document(revision: PlanRevision, *, content: dict | None) -> dict:
    """A durable ACTIVE-plan pointer, with or without the content field."""
    document = {
        "schema": "forge.revision.active-plan/1",
        "work_id": revision.work_id,
        "plan_id": revision.plan_id,
        "active_revision": revision.revision,
        "plan_digest": plan_digest(revision),
        "revised_from_digest": "e" * 64,
        "work_contract_digest": revision.work_contract_digest,
        "authorization_epoch": 3,
        "publication_epoch": 8,
        "activated_by_decision": "rd-content",
    }
    if content is not None:
        document["revision_content"] = content
    return document


async def _set_evidence(factory, run_id: str, patch: dict) -> None:
    from forge.durable import FlowRun

    async with factory() as session:
        run = await session.get(FlowRun, run_id)
        evidence = dict(run.evidence or {})
        evidence.update(patch)
        run.evidence = evidence
        await session.commit()


class TestActivationPersistsRevisionContent:
    async def test_the_switched_pointer_carries_the_content_it_digested(self, durable):
        from forge.adaptive.revisions import (
            ACTIVE_PLAN_KEY,
            REVISION_CONTENT_KEY,
            activate_pending_revision,
            stage_pending_revision,
        )

        proposed = _revision(
            _base_steps(), revision=2, parent_revision=1, summary="Reworded summary."
        )
        decision = _decision(proposed)
        await stage_pending_revision(
            durable.factory, durable.run_id, decision, proposed, _current()
        )
        assert (
            await activate_pending_revision(
                durable.factory, durable.run_id, decision.decision_id, decided_by="alice"
            )
        ).status == "activated"

        active = (await durable.evidence())[ACTIVE_PLAN_KEY]
        content = active[REVISION_CONTENT_KEY]
        # The persisted content IS the bytes the digest covers: re-parsing
        # and re-hashing reproduces the pointer's digest exactly (the
        # identity -> CONTENT join the live counterexample lacked).
        reparsed = PlanRevision.model_validate(content)
        assert plan_digest(reparsed) == active["plan_digest"]

    async def test_a_prior_version_pointer_without_content_parses_unchanged(self, durable):
        from forge.adaptive.revisions import active_plan_document_of

        record = ActivationRecord(
            decision_id="rd-x",
            work_id="wp-demo-1",
            plan_id="plan-demo-1",
            parent_revision=1,
            activated_revision=2,
            activated_plan_digest="f" * 64,
            work_contract_digest=D_CONTRACT,
            authorization_epoch=3,
            publication_epoch=8,
        )
        document = active_plan_document_of(_current(), record)
        assert "revision_content" not in document  # additive: the field is opt-in
        assert document["schema"] == "forge.revision.active-plan/1"
        assert active_plan_document_of(_current(), record, revision_content={"revision": 2})[
            "revision_content"
        ] == {"revision": 2}


class TestApprovedInputResolution:
    async def test_no_active_plan_resolves_the_spec_brief_labeled(self, durable):
        from forge.adaptive.revisions import resolve_approved_input
        from forge.durable import FlowRun

        # A run whose evidence carries NO active-plan pointer at all — a
        # run that never activated a revision (today's world, labeled).
        stranger = "c" * 32
        async with durable.factory() as session:
            session.add(FlowRun(id=stranger, project_id=1, status="planning"))
            await session.commit()
        resolved = await resolve_approved_input(
            durable.factory,
            stranger,
            task_title="Add expiry",
            task_description="Idempotent per AC-1.",
            spec_plan_text="SPEC PLAN: approach X.",
            spec_plan_digest="7" * 64,
            allowed_writes=["src/**"],
        )
        assert resolved.source == "spec"
        assert resolved.brief() == "SPEC PLAN: approach X."  # today's behavior, byte-identical
        assert resolved.plan_digest == "7" * 64
        assert resolved.allowed_writes == ("src/**",)
        assert resolved.revision_bound is False
        assert resolved.active_revision == 0 and resolved.activated_by_decision == ""

    async def test_an_active_revision_with_content_wins_the_brief(self, durable):
        from forge.adaptive.revisions import resolve_approved_input

        second = _revision(
            _base_steps(), revision=2, parent_revision=1, summary="Approach Y: validate_email."
        )
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {"active_plan": _pointer_document(second, content=second.model_dump())},
        )
        resolved = await resolve_approved_input(
            durable.factory,
            durable.run_id,
            task_title="Add expiry",
            task_description="Idempotent per AC-1.",
            spec_plan_text="SPEC PLAN: approach X (check).",
            spec_plan_digest="7" * 64,
        )
        assert resolved.source == "revision"
        assert resolved.revision_bound is True
        assert resolved.active_revision == 2
        assert resolved.plan_digest == plan_digest(second)
        brief = resolved.brief()
        assert "Approach Y: validate_email." in brief  # the ACTIVE revision's TEXT
        assert "approach X (check)" not in brief  # never the superseded spec brief
        assert plan_digest(second)[:16] in brief  # the identity the CAS switched
        assert resolved.activated_by_decision == "rd-content"

    async def test_the_reuse_decision_rides_with_its_preserved_evidence(self, durable):
        from forge.adaptive.revisions import (
            CHECKPOINT_REUSE_DECISION_KEY,
            resolve_approved_input,
        )

        second = _revision(_base_steps(), revision=2, parent_revision=1)
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {
                "active_plan": _pointer_document(second, content=second.model_dump()),
                CHECKPOINT_REUSE_DECISION_KEY: {
                    "schema": "forge.checkpoint.reuse-decision/1",
                    "activated_revision": 2,
                    "plan_digest": plan_digest(second),
                    "route": "preserve",
                    "route_reason": "compatible revision",
                    "artifacts": [
                        {"artifact_id": "ckpt-1", "kind": "checkpoint", "decision": "preserve"},
                        {
                            "artifact_id": "verif-1",
                            "kind": "verification",
                            "decision": "invalidate",
                        },
                    ],
                },
            },
        )
        resolved = await resolve_approved_input(
            durable.factory,
            durable.run_id,
            task_title="T",
            task_description="D",
            spec_plan_text="SPEC",
            spec_plan_digest="7" * 64,
        )
        assert resolved.evidence_refs == ("ckpt-1",)  # preserved only
        assert resolved.wip_reuse["route"] == "preserve"
        assert "ckpt-1" in resolved.brief()  # the brief names the standing evidence
        assert "WIP reuse route: preserve" in resolved.brief()

    async def test_a_tampered_content_refuses_with_the_typed_code(self, durable):
        import pytest as _pytest

        from forge.adaptive.revisions import RevisionRebindRefused, resolve_approved_input

        second = _revision(_base_steps(), revision=2, parent_revision=1)
        content = second.model_dump()
        content["summary"] = "TAMPERED AFTER THE CAS"
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {"active_plan": _pointer_document(second, content=content)},
        )
        with _pytest.raises(RevisionRebindRefused) as caught:
            await resolve_approved_input(
                durable.factory,
                durable.run_id,
                task_title="T",
                task_description="D",
                spec_plan_text="SPEC",
                spec_plan_digest="7" * 64,
            )
        assert caught.value.code == "content_digest_mismatch"
        assert caught.value.detail.startswith("the durable revision content digests to")

    async def test_a_prior_version_pointer_resolves_the_labeled_legacy_adapter(self, durable):
        from forge.adaptive.revisions import resolve_approved_input

        second = _revision(_base_steps(), revision=2, parent_revision=1)
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {"active_plan": _pointer_document(second, content=None)},
        )
        resolved = await resolve_approved_input(
            durable.factory,
            durable.run_id,
            task_title="T",
            task_description="D",
            spec_plan_text="SPEC PLAN (the pre-rebind brief)",
            spec_plan_digest="7" * 64,
        )
        assert resolved.source == "spec-legacy"  # labeled, never a silent re-derivation
        assert resolved.brief() == "SPEC PLAN (the pre-rebind brief)"
        assert resolved.active_revision == 2  # the identity is still on record
        assert resolved.plan_digest == plan_digest(second)

    async def test_two_workers_resolve_the_identical_document(self, durable, tmp_path):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from forge.adaptive.revisions import resolve_approved_input
        from forge.durable import FlowRun
        from forge.models.base import Base

        second = _revision(
            _base_steps(), revision=2, parent_revision=1, summary="Approach Y: validate_email."
        )
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {"active_plan": _pointer_document(second, content=second.model_dump())},
        )
        # A RESTARTED worker: a genuinely fresh engine over the same rows.
        db_path = tmp_path / "restart.db"
        source = durable.factory.kw["bind"]
        async with source.connect() as connection:
            await connection.exec_driver_sql("ATTACH DATABASE ? AS copy", (str(db_path),))
            await connection.exec_driver_sql(
                "CREATE TABLE copy.flow_runs AS SELECT * FROM flow_runs"
            )
            await connection.commit()
        engine_b = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine_b.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        try:
            first = await resolve_approved_input(
                durable.factory,
                durable.run_id,
                task_title="T",
                task_description="D",
                spec_plan_text="SPEC",
                spec_plan_digest="7" * 64,
                allowed_writes=["src/**"],
            )
            again = await resolve_approved_input(
                factory_b,
                durable.run_id,
                task_title="T",
                task_description="D",
                spec_plan_text="SPEC",
                spec_plan_digest="7" * 64,
                allowed_writes=["src/**"],
            )
            assert first.document() == again.document()
            assert first.brief() == again.brief()
            assert FlowRun is not None  # the durable row is the only difference
        finally:
            await engine_b.dispose()


class TestStandingGuidancePromotion:
    """Q39-02 (#321) — the #313 cycle-2 remedy formalized: an accepted
    STANDING steer promotes into durable revision content through the
    EXISTING approval route; a next-turn hint never expands authority."""

    def test_the_classification_vocabulary_is_closed_and_fail_closed(self):
        from forge.adaptive.revisions import is_standing_guidance

        assert is_standing_guidance(
            "Operator steer, standing direction for this continuation: use validate_email"
        )
        assert is_standing_guidance("From now on the entrypoint is validate_email.")
        assert is_standing_guidance("Going forward, always use the new schema.")
        assert not is_standing_guidance("fix the failing assertion first")
        assert not is_standing_guidance("")
        assert not is_standing_guidance("use the existing helper here")

    def test_the_promoted_proposal_keeps_steps_byte_identical(self):
        from forge.adaptive.revisions import standing_guidance_revision

        active = _revision(_base_steps(), revision=2, parent_revision=1)
        proposed = standing_guidance_revision(
            active,
            "standing direction: the entrypoint is validate_email",
            command_id="cmd-steer-1",
        )
        assert proposed is not None
        assert proposed.revision == 3
        assert proposed.parent_revision == 2
        assert proposed.steps == active.steps  # WIP compatibility survives by construction
        assert "standing direction: the entrypoint is validate_email" in proposed.summary
        assert "cmd-steer-1" in proposed.summary  # provenance rides the content
        assert plan_digest(proposed) != plan_digest(active)

    def test_a_next_turn_hint_promotes_to_nothing(self):
        from forge.adaptive.revisions import standing_guidance_revision

        active = _revision(_base_steps(), revision=2, parent_revision=1)
        assert standing_guidance_revision(active, "fix the test first") is None

    async def test_the_promotion_stages_through_the_existing_approval_route(self, durable):
        from forge.adaptive.revisions import (
            ACTIVE_PLAN_KEY,
            PENDING_PROPOSAL_KEY,
            REVISION_CONTENT_KEY,
            activate_pending_revision,
            stage_standing_guidance_promotion,
        )

        second = _revision(
            _base_steps(), revision=2, parent_revision=1, summary="Approach X: entrypoint check."
        )
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {"active_plan": _pointer_document(second, content=second.model_dump())},
        )
        decision_id = await stage_standing_guidance_promotion(
            durable.factory,
            durable.run_id,
            "standing direction: the entrypoint is validate_email, not check",
            command_id="cmd-steer-1",
        )
        assert decision_id
        evidence = await durable.evidence()
        staged = evidence[PENDING_PROPOSAL_KEY]  # the human gate owns it now
        assert staged["decision"]["decision_id"] == decision_id
        assert staged["decision"]["parent_revision"] == 2
        assert staged["decision"]["proposed_digest"] == plan_digest(
            PlanRevision.model_validate(staged["proposed"])
        )
        assert "validate_email" in staged["proposed"]["summary"]
        # The ACTIVE plan is UNCHANGED until the human approves: nothing
        # that was not approved expanded any authority.
        assert evidence[ACTIVE_PLAN_KEY]["active_revision"] == 2

        outcome = await activate_pending_revision(
            durable.factory, durable.run_id, decision_id, decided_by="alice"
        )
        assert outcome.status == "activated"
        after = (await durable.evidence())[ACTIVE_PLAN_KEY]
        assert after["active_revision"] == 3
        assert (
            plan_digest(PlanRevision.model_validate(after[REVISION_CONTENT_KEY]))
            == (after["plan_digest"])
        )
        assert "validate_email" in after[REVISION_CONTENT_KEY]["summary"]

    async def test_an_unapproved_hint_stages_nothing_and_changes_no_brief(self, durable):
        from forge.adaptive.revisions import (
            PENDING_PROPOSAL_KEY,
            resolve_approved_input,
            stage_standing_guidance_promotion,
        )

        second = _revision(_base_steps(), revision=2, parent_revision=1)
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {"active_plan": _pointer_document(second, content=second.model_dump())},
        )
        assert (
            await stage_standing_guidance_promotion(
                durable.factory, durable.run_id, "just fix the test", command_id="cmd-x"
            )
            is None
        )
        evidence = await durable.evidence()
        assert PENDING_PROPOSAL_KEY not in evidence  # nothing staged, nothing expanded
        resolved = await resolve_approved_input(
            durable.factory,
            durable.run_id,
            task_title="T",
            task_description="D",
            spec_plan_text="SPEC",
            spec_plan_digest="7" * 64,
        )
        assert "fix the test" not in resolved.brief()

    async def test_a_pointer_without_content_never_re_derives_plan_bytes(self, durable):
        from forge.adaptive.revisions import stage_standing_guidance_promotion

        second = _revision(_base_steps(), revision=2, parent_revision=1)
        await _set_evidence(
            durable.factory,
            durable.run_id,
            {"active_plan": _pointer_document(second, content=None)},
        )
        assert (
            await stage_standing_guidance_promotion(
                durable.factory,
                durable.run_id,
                "standing direction: anything",
                command_id="cmd-x",
            )
            is None
        )


# ----------------------------------------------------------------------
# R40-02 (#338) — the classic-run adapter behind the review round
# ----------------------------------------------------------------------


class TestClassicRunAdapter:
    """The verified adapter: frozen spec + accepted request → the round's
    initial approved-input representation — nothing else contributes."""

    @staticmethod
    def _spec(**overrides) -> dict:
        base = {
            "task_title": "Add the validator",
            "task_description": "Validate emails on entry.",
            "plan_summary": "Create forge-demo/validator.py with the entry hook.",
            "plan_digest": "p" * 64,
            "policy_digest": "q" * 64,
            "allowed_paths": ["forge-demo/**"],
            "source_base_oid": "base-sha-1",
            # noise the adapter must IGNORE (live settings never shape a
            # derived plan — only the closed field set does)
            "backend": "ci_harness",
            "harness_driver": "claude-code",
        }
        base.update(overrides)
        return base

    @staticmethod
    def _request():
        from forge.adaptive.revisions import ReviewFeedbackRequest

        return ReviewFeedbackRequest(
            note_id="9901",
            run_id="c" * 32,
            discussion_id="d-x",
            mr_iid=7,
            actor="alice",
            head_sha="h" * 40,
            classification="in_scope_correction",
            text="handle `forge-demo/validator.py` empty input",
            referenced_paths=("forge-demo/validator.py",),
        )

    def test_the_adapter_reads_only_the_closed_spec_field_set(self):
        from forge.adaptive.revisions import classic_spec_revision

        noisy = dict(self._spec(), backend="SOMETHING-ELSE", harness_driver="other-driver")
        clean = self._spec()
        assert plan_digest(
            classic_spec_revision(work_id="c" * 32, spec=noisy, request=self._request())
        ) == plan_digest(
            classic_spec_revision(work_id="c" * 32, spec=clean, request=self._request())
        )

    def test_the_seed_round_trips_through_the_content_join(self):
        from forge.adaptive.revisions import (
            REVISION_CONTENT_KEY,
            classic_spec_revision,
            round_active_plan_seed,
        )

        revision = classic_spec_revision(
            work_id="c" * 32, spec=self._spec(), request=self._request()
        )
        seed = round_active_plan_seed(
            revision, revised_from_digest="p" * 64, decision_id="rd-review-x"
        )
        # the content parses back and digests identically — the #321 join
        back = PlanRevision.model_validate(seed[REVISION_CONTENT_KEY])
        assert plan_digest(back) == seed["plan_digest"]
        assert back.work_id == "c" * 32
