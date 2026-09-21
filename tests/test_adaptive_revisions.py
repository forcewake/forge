"""The revision lifecycle rules — PLN-04..PLN-08 (review 05868e9).

``test_adaptive_contracts.py`` proves the SHAPES parse; these tests prove
the RULES: tactical revisions land inside the pre-approved bounds and
material ones never slip through, approvals compare-and-swap on their
epoch, questions block instead of defaulting, evidence is superseded
precisely rather than deleted, and decision records stay secret-free.
"""

from __future__ import annotations

import pytest

from forge.adaptive.models import PlanRevision, WorkContract
from forge.adaptive.revisions import (
    Question,
    RevisionDecision,
    activate_revision,
    apply_tactical,
    change_log,
    classify_revision,
    decision_record,
    fresh_session_brief,
    invalidation_set,
    plan_digest,
    route_question,
    stale_callback_guard,
)

D_CONTRACT = "1" * 64
D_SNAPSHOT = "2" * 64
D_STALE = "9" * 64


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


def _decision(**overrides) -> RevisionDecision:
    base = {
        "decision_id": "rd-1",
        "work_id": "wp-demo-1",
        "parent_revision": 1,
        "proposed_revision_id": "plan-demo-1#2",
        "work_contract_digest": D_CONTRACT,
        "authorization_epoch": 3,
    }
    return RevisionDecision(**(base | overrides))


def _question(**overrides) -> Question:
    base = {
        "question_id": "Q1",
        "work_id": "wp-demo-1",
        "reason": "Migration authority for the expiry dedup key is unclear.",
    }
    return Question(**(base | overrides))


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
        with pytest.raises(ValueError, match="material change requires approval"):
            apply_tactical(old, material, contract)


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
    def test_approved_decision_activates_the_proposed_revision(self):
        decision = _decision().approve("approver", 3)
        proposed = _revision(_base_steps(), revision=2)
        activated = activate_revision(decision, proposed)
        assert activated.parent_revision == decision.parent_revision
        assert activated.revision == 2

    def test_rejected_decision_leaves_the_proposal_unactivated(self):
        rejected = _decision().reject("approver", 3, "scope widened without evidence")
        proposed = _revision(_base_steps(), revision=2)
        with pytest.raises(ValueError, match="rejected"):
            activate_revision(rejected, proposed)
        # the parent revision stays the honest, checkpoint-usable state
        assert proposed.parent_revision is None

    def test_expired_and_undecided_never_activate(self):
        with pytest.raises(ValueError, match="expired"):
            activate_revision(_decision().expire(), _revision(_base_steps(), revision=2))
        with pytest.raises(ValueError, match="undecided"):
            activate_revision(_decision(), _revision(_base_steps(), revision=2))

    def test_proposal_must_follow_the_decisions_parent(self):
        decision = _decision().approve("approver", 3)
        with pytest.raises(ValueError, match="must follow"):
            activate_revision(decision, _revision(_base_steps(), revision=1))


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
