"""Retained view lessons use current SQL receipts and authenticated controls.

The old dictionary report serializers, stored projection/CAS DTO and advisory
role/action tickets are retired. Run state, independent receipts, exact current
leases and closed operator requests carry their safety lessons directly.
"""

from datetime import datetime, timedelta
from decimal import Decimal
import json

from forge_protocol import digest
import pytest
from sqlalchemy.exc import IntegrityError

from forge.command_values import CommandResult, render
from forge.domain.budget import Observation
from forge.domain.states import State
from forge.store.models_commands import EngineCommand
from forge.store.models_lane import EngineReviewRound
from forge.store.models_runs import EngineRun
from forge.store.records import RunRequest, StoreConflict
from forge.store.runs import RunStore
from tests.operator_evidence_fixture import (
    BASE,
    SHA,
    candidate,
    checkpoint,
    command_row,
    control,
    edit_run,
    effect,
    mark_step,
    round_child,
)
from tests.operator_surface_fixture import surface as surface, detail, headers, model_rows, seed_run
from tests.operator_view_lesson_fixture import (
    approval,
    basic,
    closed_hint,
    command,
    failed_plan,
    pool,
    view,
)


class TestStateDerivation:
    async def test_a_bare_run_row_derives_requested(self, surface):
        run, document = await basic(surface, "queued")
        assert document["state"] == "queued"
        assert document["evidence"]["state"] == document["state"]
        assert document["evidence"]["run_version"] == document["version"]

    async def test_an_approval_derives_authorized(self, surface):
        run = await seed_run(surface, state="awaiting_approval")
        approved, gate = await approval(surface, run)
        document = await view(surface, run)
        assert document["state"] == "awaiting_approval"
        assert document["evidence"]["identity"]["approved_digest"] == approved.approved_digest
        assert document["evidence"]["identity"]["gates"]["latest"][0]["id"] == gate.id

    async def test_an_executing_attempt_derives_executing(self, surface):
        run, document = await basic(surface, "running")
        assert document["state"] == "running"
        assert document["evidence"]["state"] == document["state"]
        assert document["evidence"]["run_version"] == document["version"]

    @pytest.mark.parametrize("control_state", ["pending", "acknowledged", "refused", "expired"])
    async def test_a_commanded_pause_without_a_committed_checkpoint_is_pause_pending(
        self, surface, control_state
    ):
        run = await seed_run(surface, state="running")
        row = await control(surface, run, state=control_state)
        document = await view(surface, run)
        assert document["state"] == "running"
        assert document["evidence"]["controls"]["latest"][0]["state"] == control_state
        assert document["evidence"]["controls"]["latest"][0]["id"] == row.id
        assert document["evidence"]["checkpoints"]["latest"] == []

    async def test_a_committed_checkpoint_with_the_fence_held_is_safely_paused(self, surface):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        saved = await checkpoint(surface, run)
        await control(surface, run, state="acknowledged", checkpoint_id=saved.id)
        document = await view(surface, run)
        assert document["reason"] == "lane_paused"
        assert document["evidence"]["controls"]["latest"][0]["checkpoint_id"] == saved.id
        assert document["evidence"]["checkpoints"]["latest"][0]["blob_availability"] == "unobserved"

    async def test_safely_paused_holds_via_the_router_pairing_without_an_explicit_fence_word(
        self, surface
    ):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        saved = await checkpoint(surface, run)
        await control(surface, run, state="acknowledged", checkpoint_id=saved.id)
        facts = (await view(surface, run))["evidence"]
        assert facts["controls"]["latest"][0]["acknowledged_status"] == "applied"
        assert facts["lane"] is None
        assert facts["checkpoints"]["latest"][0]["retained"] is True

    async def test_an_explicitly_cleared_fence_is_not_safely_paused(self, surface):
        run = await seed_run(surface, state="running", generation=2)
        await checkpoint(surface, run, retained=False)
        document = await view(surface, run)
        assert document["state"] == "running"
        assert (
            document["evidence"]["checkpoints"]["latest"][0]["blob_availability"] == "not_retained"
        )

    async def test_an_activated_resume_is_resumed(self, surface):
        run = await seed_run(surface, state="running", generation=2, reason="operator_resume")
        await command_row(surface, run, kind="resume", state="completed", reason="resume_scheduled")
        document = await view(surface, run)
        assert document["generation"] == 2 and document["reason"] == "operator_resume"
        assert document["evidence"]["commands"]["latest"][0]["reason"] == "resume_scheduled"
        assert document["evidence"]["lane"] is None

    async def test_a_resume_request_alone_is_not_restored(self, surface):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        await checkpoint(surface, run)
        await command_row(surface, run, kind="resume", state="resolved", reason="resume_scheduled")
        document = await view(surface, run)
        assert document["state"] == "blocked" and document["reason"] == "lane_paused"
        assert document["evidence"]["commands"]["latest"][0]["state"] == "resolved"
        assert document["evidence"]["lane"] is None

    async def test_an_undrained_resume_command_stays_safely_paused(self, surface):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        await checkpoint(surface, run)
        await command_row(surface, run, kind="resume", state="accepted", reason="resume_scheduled")
        document = await view(surface, run)
        assert document["state"] == "blocked" and document["reason"] == "lane_paused"
        # An undecided command has no authorized run binding yet.
        assert document["evidence"]["commands"]["latest"] == []
        assert document["evidence"]["lane"] is None

    async def test_a_candidate_without_a_passed_verification_is_unverified(self, surface):
        run = await seed_run(surface, state="verifying")
        await candidate(surface, run, verification=None)
        document = await view(surface, run)
        facts = document["evidence"]["candidates"][0]
        assert facts["verification"] is None
        assert document["state"] == "verifying"
        assert document["evidence"]["acceptance"]["state"] == "unobserved"

    async def test_a_passed_verification_makes_the_candidate_verified_ready(self, surface):
        run = await seed_run(surface, state="verifying")
        await candidate(surface, run, verification="passed")
        document = await view(surface, run)
        facts = document["evidence"]["candidates"][0]
        assert (
            facts["verification"]["status"] == "passed"
            and facts["verification"]["bound_to_candidate"]
        )
        assert document["state"] == "verifying"
        assert document["evidence"]["acceptance"]["state"] == "unobserved"

    async def test_a_failed_verification_does_not_make_the_candidate_ready(self, surface):
        run = await seed_run(surface, state="verifying")
        await candidate(surface, run, verification="failed")
        document = await view(surface, run)
        facts = document["evidence"]["candidates"][0]
        assert (
            facts["verification"]["status"] == "failed"
            and facts["verification"]["bound_to_candidate"]
        )
        assert document["state"] == "verifying"
        assert document["evidence"]["acceptance"]["state"] == "unobserved"

    async def test_a_pass_for_an_earlier_candidate_is_history_not_readiness(self, surface):
        run = await seed_run(surface, state="verifying")
        await candidate(surface, run, verification="passed")
        await edit_run(surface, run, generation=2, version=2)
        await candidate(surface, run, generation=2, sha="e" * 40)
        rows = (await view(surface, run))["evidence"]["candidates"]
        assert rows[0]["current"] and rows[0]["verification"] is None
        assert not rows[1]["current"] and rows[1]["verification"]["status"] == "passed"

    async def test_a_pass_for_the_last_member_is_current_readiness(self, surface):
        run = await seed_run(surface, state="verifying", generation=2)
        await candidate(surface, run, generation=1)
        await candidate(surface, run, generation=2, sha="e" * 40, verification="passed")
        rows = (await view(surface, run))["evidence"]["candidates"]
        assert rows[0]["current"] and rows[0]["verification"]["tested_sha"] == "e" * 40
        assert not rows[1]["current"]

    async def test_an_explicit_active_candidate_pointer_wins_over_the_list_order(self, surface):
        run = await seed_run(surface, state="verifying", generation=1)
        await candidate(surface, run, generation=2, sha="e" * 40, verification="passed")
        await candidate(surface, run, generation=1)
        rows = (await view(surface, run))["evidence"]["candidates"]
        assert rows[0]["generation"] == 2 and not rows[0]["current"]
        assert rows[1]["current"] and rows[1]["verification"] is None

    async def test_the_candidate_history_renders_beside_the_current_one(self, surface):
        run = await seed_run(surface, generation=2)
        await candidate(surface, run, generation=1)
        await candidate(surface, run, generation=2, sha="e" * 40)
        facts = (await view(surface, run))["evidence"]
        assert facts["candidates_complete"]
        assert {row["sha"] for row in facts["candidates"]} == {SHA, "e" * 40}

    async def test_a_failed_attempt_derives_rejected(self, surface):
        run, document = await basic(surface, "failed")
        assert document["state"] == "failed"
        assert document["evidence"]["state"] == document["state"]
        assert document["evidence"]["run_version"] == document["version"]

    async def test_a_cancelled_run_row_derives_cancelled(self, surface):
        run, document = await basic(surface, "cancelled")
        assert document["state"] == "cancelled"
        assert document["evidence"]["state"] == document["state"]
        assert document["evidence"]["run_version"] == document["version"]

    async def test_a_failed_run_row_derives_rejected(self, surface):
        run, document = await basic(surface, "failed")
        assert document["state"] == "failed"
        assert document["evidence"]["state"] == document["state"]
        assert document["evidence"]["run_version"] == document["version"]

    async def test_a_cancel_request_derives_cancelled_even_before_the_terminal_row(self, surface):
        run = await seed_run(
            surface, state="publishing", cancel_requested=True, cancellation_generation=1
        )
        document = await view(surface, run)
        assert document["state"] == "publishing" and document["cancel_requested"]
        assert document["evidence"]["cancel_requested"]
        assert (await command(surface, "retry", argument=run.id))["reason"] == "cancelled"

    async def test_an_acceptance_marker_derives_accepted(self, surface):
        run = await seed_run(surface, state="ready_for_human")
        await candidate(surface, run, verification="passed", review="approve")
        document = await view(surface, run)
        assert document["state"] == "ready_for_human"
        assert document["evidence"]["acceptance"] == {"state": "unobserved"}
        assert "accepted" not in document

    async def test_the_ladder_orders_pause_above_candidate_states(self, surface):
        run = await seed_run(surface, state="running")
        await candidate(surface, run, verification="passed")
        await control(surface, run)
        facts = (await view(surface, run))["evidence"]
        assert facts["controls"]["latest"][0]["state"] == "pending"
        assert facts["candidates"][0]["verification"]["status"] == "passed"
        assert facts["state"] == "running"


class TestWedged:
    async def test_executing_with_no_semantic_transition_beyond_the_threshold_is_wedged(
        self, surface
    ):
        run = await seed_run(surface, state="running")
        expired = surface.service.budget.clock.now() - timedelta(seconds=1)
        await mark_step(
            surface,
            run,
            state="running",
            lease_owner="worker",
            lease_epoch=1,
            lease_expires_at=expired,
        )
        document = await view(surface, run)
        step = document["evidence"]["steps"]["latest"][0]
        assert datetime.fromisoformat(step["lease_expires_at"]) == expired
        assert step["state"] == "running" and document["state"] == "running"
        assert "wedged" not in document

    async def test_a_fresh_transition_keeps_it_executing(self, surface):
        run = await seed_run(surface, state="running")
        expiry = surface.service.budget.clock.now() + timedelta(seconds=60)
        await mark_step(
            surface,
            run,
            state="running",
            lease_owner="worker",
            lease_epoch=1,
            lease_expires_at=expiry,
        )
        document = await view(surface, run)
        assert document["state"] == "running"
        assert (
            datetime.fromisoformat(document["evidence"]["steps"]["latest"][0]["lease_expires_at"])
            == expiry
        )

    async def test_the_threshold_is_a_parameter(self, surface):
        run = await seed_run(surface, state="running")
        assert (await detail(surface, run, wedged_after=1)).status_code == 400
        assert (await detail(surface, run, wedged_after=99999)).status_code == 400
        assert (await view(surface, run))["state"] == "running"

    async def test_wedged_needs_a_readable_clock(self, surface):
        run = await seed_run(surface, state="running")
        step = (await view(surface, run))["evidence"]["steps"]["latest"][0]
        assert step["lease_expires_at"] is None
        assert "execution_seconds" not in step and "wedged" not in step

    async def test_only_executing_wedges(self, surface):
        run = await seed_run(surface, state="verifying")
        await mark_step(
            surface,
            run,
            state="succeeded",
            updated_at=surface.service.budget.clock.now() - timedelta(days=1),
        )
        await candidate(surface, run)
        document = await view(surface, run)
        assert (
            document["state"] == "verifying"
            and document["evidence"]["steps"]["latest"][0]["state"] == "succeeded"
        )
        assert "wedged" not in document

    async def test_a_command_transition_resets_the_wedged_clock(self, surface):
        run = await seed_run(surface, state="running")
        expiry = surface.service.budget.clock.now() - timedelta(seconds=1)
        await mark_step(
            surface,
            run,
            state="running",
            lease_owner="worker",
            lease_epoch=1,
            lease_expires_at=expiry,
        )
        await command_row(surface, run, kind="steer", state="completed")
        step = (await view(surface, run))["evidence"]["steps"]["latest"][0]
        assert datetime.fromisoformat(step["lease_expires_at"]) == expiry
        assert "execution_seconds" not in step


class TestStaleAndDead:
    async def test_a_stored_projection_older_than_the_rows_derives_stale(self, surface):
        run = await seed_run(surface)
        old = await view(surface, run)
        await edit_run(surface, run, state="planning", version=2)
        current = await view(surface, run)
        assert old["version"] == 1 and current["version"] == current["evidence"]["run_version"] == 2
        assert current["state"] == "planning"

    async def test_a_fresh_stored_projection_derives_no_stale(self, surface):
        run = await seed_run(surface)
        assert await view(surface, run) == await view(surface, run)
        assert "projection_version" not in await view(surface, run)

    async def test_a_failed_run_with_unresolved_effects_is_dead(self, surface):
        run = await seed_run(surface, state="failed")
        await effect(surface, run, state="unknown")
        document = await view(surface, run)
        assert document["state"] == "failed"
        assert document["evidence"]["effects"][0]["state"] == "unknown"
        assert document["evidence"]["effects_complete"]

    async def test_a_failed_run_with_resolved_effects_stays_rejected(self, surface):
        run = await seed_run(surface, state="failed")
        await effect(surface, run, state="failed")
        document = await view(surface, run)
        assert (
            document["state"] == "failed"
            and document["evidence"]["effects"][0]["state"] == "failed"
        )

    async def test_dead_evidence_names_the_unreconciled_effects(self, surface):
        run = await seed_run(surface, state="cancelled")
        await effect(surface, run, state="requested", key="exact-owned-effect")
        facts = (await view(surface, run))["evidence"]
        assert facts["effects"] == [
            {"key": "exact-owned-effect", "kind": "post_comment", "state": "requested"}
        ]
        assert facts["effects_complete"]


class TestEvidenceLinks:
    async def test_every_derivation_carries_row_digest_evidence(self, surface):
        run = await seed_run(surface)
        saved = await candidate(surface, run)
        cp = await checkpoint(surface, run)
        facts = (await view(surface, run))["evidence"]
        assert (
            facts["candidates"][0]["id"] == saved.id
            and facts["candidates"][0]["digest"] == saved.digest
        )
        assert facts["checkpoints"]["latest"][0]["digest"] == cp.digest
        assert facts["steps"]["latest"][0]["id"]

    async def test_every_state_of_the_vocabulary_derives(self, surface):
        for index, state in enumerate(State):
            run = await seed_run(surface, number=20 + index, state=state.value)
            document = await view(surface, run)
            assert document["state"] == document["evidence"]["state"] == state.value

    async def test_a_projection_without_a_run_row_is_nothing(self, surface):
        response = await detail(surface, "0" * 32)
        assert response.status_code == 404
        assert response.json() == {"detail": "run not found"}


class TestProjectionAndCas:
    async def test_the_initial_projection_is_version_one(self, surface):
        run, document = await basic(surface)
        assert document["version"] == document["evidence"]["run_version"] == 1
        assert document["evidence"]["steps"]["complete"]

    async def test_apply_update_mints_the_next_version(self, surface):
        run = await seed_run(surface)
        await RunStore(surface.service.factory).transition(
            run.id, expected_version=1, target=State.PLANNING
        )
        document = await view(surface, run)
        assert document["version"] == 2 and document["state"] == "planning"

    async def test_a_delayed_replay_is_rejected_and_the_newer_projection_kept(self, surface):
        run = await seed_run(surface)
        store = RunStore(surface.service.factory)
        await store.transition(run.id, expected_version=1, target=State.PLANNING)
        with pytest.raises(StoreConflict):
            await store.transition(run.id, expected_version=1, target=State.FAILED)
        document = await view(surface, run)
        assert document["version"] == 2 and document["state"] == "planning"

    async def test_an_update_ahead_of_the_stored_version_is_a_bug(self, surface):
        run = await seed_run(surface)
        with pytest.raises(StoreConflict):
            await RunStore(surface.service.factory).transition(
                run.id, expected_version=7, target=State.PLANNING
            )
        assert (await view(surface, run))["version"] == 1

    async def test_versions_are_strictly_monotonic_across_a_chain(self, surface):
        run = await seed_run(surface)
        store = RunStore(surface.service.factory)
        versions = [run.version]
        for version, state in enumerate(
            (State.PLANNING, State.AWAITING_APPROVAL, State.RUNNING), 1
        ):
            current = await store.transition(run.id, expected_version=version, target=state)
            versions.append(current.version)
        assert versions == [1, 2, 3, 4]
        assert (await view(surface, run))["version"] == 4

    async def test_an_update_cannot_switch_runs(self, surface):
        run = await seed_run(surface)
        with pytest.raises(StoreConflict):
            await RunStore(surface.service.factory).admit(
                RunRequest(run.id, "github", "alpha", "123", "issue", "7")
            )
        assert (await view(surface, run))["provider"] == "gitlab"

    async def test_the_stale_rejection_names_the_current_state_and_safe_next_action(self, surface):
        run = await seed_run(surface)
        await edit_run(surface, run, state="cancelled", cancel_requested=True, version=2)
        result = await command(surface, "retry", argument=run.id)
        assert result["reason"] == "cancelled"
        assert "start new work" in render(CommandResult.model_validate(result))
        assert (await view(surface, run))["state"] == "cancelled"


class TestRecoveryActionsMatrix:
    async def test_resume_is_valid_only_in_safely_paused(self, surface):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        await checkpoint(surface, run)
        result = await command(surface, "resume")
        assert result["status"] == "refused" and result["reason"] == "checkpoint_unavailable"
        assert (await view(surface, run))["state"] == "blocked"

    async def test_steer_is_valid_only_while_executing(self, surface):
        run = await seed_run(surface, state="awaiting_approval")
        result = await command(surface, "steer", argument="bounded instruction")
        assert result["status"] == "refused" and result["reason"] == "lane_not_active"
        assert (await view(surface, run))["state"] == "awaiting_approval"

    async def test_retry_is_valid_only_in_failed_terminals(self, surface):
        run = await failed_plan(surface)
        result = await command(surface, "retry", argument=run.id)
        assert result["reason"] == "retry_scheduled"
        assert (await view(surface, run))["state"] == "planning"
        assert (await command(surface, "retry", argument=run.id))["reason"] == "not_retryable"

    async def test_a_stale_view_allows_only_the_probe(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "expected_version", 0)
        assert (await detail(surface, run)).status_code == 200

    async def test_an_accepted_run_needs_nothing(self, surface):
        run = await seed_run(surface, state="ready_for_human")
        document = await view(surface, run)
        assert (
            "actions" not in document
            and document["evidence"]["acceptance"]["state"] == "unobserved"
        )
        assert (await command(surface, "retry", argument=run.id))["reason"] == "not_retryable"

    async def test_observers_and_automation_may_only_probe(self, surface):
        run = await seed_run(surface)
        assert (await command(surface, "status", argument=run.id, name="observer"))[
            "status"
        ] == "observed"
        assert (await command(surface, "cancel", argument=run.id, name="observer"))[
            "reason"
        ] == "actor_not_authorized"
        response = await surface.client.get(
            "/api/v1/runs", headers={"Authorization": "Bearer unconfigured-automation"}
        )
        assert response.status_code == 401

    async def test_unknown_states_and_roles_fail_visibly(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "actor_role", "administrator-from-client")
        async with surface.service.factory() as session:
            row = await session.get(EngineRun, run.id)
            row.state = "invented-state"
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
        assert (await view(surface, run))["state"] == "queued"

    async def test_every_state_offers_the_probe_to_approvers(self, surface):
        for index, state in enumerate(State):
            run = await seed_run(surface, number=20 + index, state=state.value)
            assert (await detail(surface, run)).status_code == 200
        assert not surface.peer.calls and not surface.gateway.writes


class TestRecoveryActionPlanning:
    async def test_plan_carries_the_four_facts_and_the_cas_ticket(self, surface):
        run = await seed_run(surface)
        result = await command(surface, "status", argument=run.id)
        rows = await model_rows(surface, EngineCommand)
        wire = json.loads(rows[0].document_json)
        assert (
            wire["actor"]["native_id"] == "42"
            and wire["target"]["repo"]["identity"]["repository_id"] == "123"
        )
        assert result["run_id"] == run.id and json.loads(result["data_json"])["run_version"] == 1

    async def test_the_action_digest_names_the_exact_artifact_in_priority_order(self, surface):
        run = await seed_run(surface)
        approved, _ = await approval(surface, run)
        cp = await checkpoint(surface, run)
        await candidate(surface, run)
        facts = (await view(surface, run))["evidence"]
        assert facts["identity"]["approved_digest"] == approved.approved_digest
        assert (
            facts["checkpoints"]["latest"][0]["digest"] == cp.digest
            and facts["candidates"][0]["sha"] == SHA
        )
        assert "action_digest" not in facts

    async def test_plan_refuses_unknown_roles(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "role", "approver")
        assert (await command(surface, "cancel", argument=run.id, name="observer"))[
            "reason"
        ] == "actor_not_authorized"


class TestRecoveryActionDecisions:
    async def test_a_fresh_matching_action_is_allowed(self, surface):
        run = await seed_run(surface)
        result = await command(surface, "cancel", argument=run.id)
        assert result["status"] == "accepted" and result["reason"] == "cancelled"
        assert (await view(surface, run))["cancel_requested"]

    async def test_a_stale_command_from_an_old_status_comment_is_refused_with_the_current_state(
        self, surface
    ):
        run = await seed_run(surface, state="running")
        await command(surface, "status", argument=run.id)
        await edit_run(surface, run, state="cancelled", cancel_requested=True, version=2)
        result = await command(surface, "retry", argument=run.id)
        assert result["reason"] == "cancelled"
        assert (await view(surface, run))["version"] == 2

    async def test_a_command_ahead_of_the_stored_version_is_refused(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "expected_version", 9999)

    async def test_a_state_changed_command_is_refused_with_the_safe_next_action(self, surface):
        run = await seed_run(surface, state="running")
        await edit_run(surface, run, state="cancelled", cancel_requested=True, version=2)
        result = await command(surface, "retry", argument=run.id)
        assert result["reason"] == "cancelled"
        assert "start new work" in render(CommandResult.model_validate(result))

    async def test_an_observer_pause_is_refused(self, surface):
        run = await seed_run(surface, state="running")
        result = await command(surface, "pause", name="observer")
        assert result["reason"] == "actor_not_authorized"
        assert (await view(surface, run))["evidence"]["controls"]["latest"] == []


class TestRender:
    async def test_render_carries_every_exact_identity(self, surface):
        run = await seed_run(surface)
        approved, gate = await approval(surface, run)
        await candidate(surface, run)
        document = await view(surface, run)
        assert (document["id"], document["connection_id"], document["repository_id"]) == (
            run.id,
            "ce-1",
            "123",
        )
        assert document["evidence"]["identity"]["spec_digest"] == approved.spec_digest
        assert document["evidence"]["identity"]["gates"]["latest"][0]["id"] == gate.id

    async def test_render_never_leaks_secret_values(self, surface):
        run = await seed_run(surface, reason="Bearer ghp_private123456789")
        await effect(surface, run, key="glpat-private-effect-key")
        response = await detail(surface, run)
        assert response.status_code == 200
        assert not any(
            value in response.text
            for value in ("ghp_private", "glpat-private", "synthetic-read", "synthetic-write")
        )

    async def test_render_never_carries_raw_prompt_text(self, surface):
        run = await seed_run(surface)
        await command_row(surface, run, kind="steer", state="completed")
        await candidate(surface, run, review="approve")
        response = await detail(surface, run)
        assert not any(
            value in response.text
            for value in (
                "raw prompt",
                "raw model",
                "private request",
                "brief_text",
                "document_json",
                "payload_json",
            )
        )

    async def test_render_lists_unresolved_external_effects(self, surface):
        run = await seed_run(surface)
        await effect(surface, run, key="first-effect", state="requested")
        await effect(surface, run, key="second-effect", state="unknown")
        rows = (await view(surface, run))["evidence"]["effects"]
        assert {(row["key"], row["state"]) for row in rows} == {
            ("first-effect", "requested"),
            ("second-effect", "unknown"),
        }

    async def test_render_reuses_the_status_projection_line(self, surface):
        run = await seed_run(surface)
        facts = (await view(surface, run))["evidence"]
        result = await command(surface, "status", argument=run.id)
        assert json.loads(result["data_json"]) == facts
        assert result["reason"] == facts["state"]

    async def test_render_carries_blocked_reason_and_thin_evidence_links(self, surface):
        run = await seed_run(surface, state="blocked", reason="verification_stale:branch_drift")
        await effect(surface, run, key="owned-proof")
        document = await view(surface, run)
        assert (
            document["reason"]
            == document["evidence"]["reason"]
            == "verification_stale:branch_drift"
        )
        assert document["evidence"]["effects"][0]["key"] == "owned-proof"

    async def test_render_keeps_unobserved_sources_explicit(self, surface):
        run = await seed_run(surface)
        await checkpoint(surface, run)
        facts = (await view(surface, run))["evidence"]
        assert facts["acceptance"]["state"] == "unobserved"
        assert facts["lane"] is None
        assert facts["checkpoints"]["latest"][0]["blob_availability"] == "unobserved"
        assert facts["effects_complete"] and facts["commands"]["complete"]


class TestExplainBlocked:
    async def test_a_revoked_authority_is_never_answered_with_retry(self, surface):
        run = await seed_run(surface, state="cancelled", cancel_requested=True)
        result = await command(surface, "retry", argument=run.id)
        assert result["reason"] == "cancelled" and result["status"] == "refused"
        assert "start new work" in render(CommandResult.model_validate(result))

    async def test_the_revocation_vocabulary_covers_the_provider_refusals(self, surface):
        run = await seed_run(surface)
        for kind in ("cancel", "retry", "pause", "resume", "steer"):
            result = await command(
                surface, kind, argument="bounded text" if kind == "steer" else "", name="observer"
            )
            assert result["reason"] == "actor_not_authorized"
        assert not (await view(surface, run))["cancel_requested"]

    async def test_a_plain_blockage_is_not_a_revocation(self, surface):
        run = await failed_plan(surface)
        assert not (await view(surface, run))["cancel_requested"]
        assert (await command(surface, "retry", argument=run.id))["reason"] == "retry_scheduled"

    async def test_each_uncertain_effect_renders_its_own_reason_with_its_row_link(self, surface):
        run = await seed_run(surface, state="failed")
        await effect(surface, run, key="lost-response", state="unknown")
        await effect(surface, run, key="not-sent", state="requested")
        rows = (await view(surface, run))["evidence"]["effects"]
        assert len(rows) == 2
        assert {r["key"]: r["state"] for r in rows} == {
            "lost-response": "unknown",
            "not-sent": "requested",
        }

    async def test_a_lost_required_checkpoint_names_restore_not_retry(self, surface):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        assert (await view(surface, run))["evidence"]["checkpoints"]["latest"] == []
        result = await command(surface, "resume")
        assert result["reason"] == "checkpoint_unavailable"
        assert (await view(surface, run))["generation"] == 1

    async def test_a_present_checkpoint_authority_is_not_a_loss(self, surface):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        saved = await checkpoint(surface, run)
        row = (await view(surface, run))["evidence"]["checkpoints"]["latest"][0]
        assert row["retained"] and row["id"] == saved.id
        assert row["blob_availability"] == "unobserved"

    async def test_an_uncertain_lease_renders_a_capacity_wait_with_its_age(self, surface):
        run = await seed_run(surface, state="running", reason="capacity_wait")
        expiry = surface.service.budget.clock.now() - timedelta(seconds=5)
        await mark_step(
            surface,
            run,
            state="running",
            lease_owner="worker",
            lease_epoch=1,
            lease_expires_at=expiry,
        )
        document = await view(surface, run)
        assert document["reason"] == "capacity_wait"
        assert (
            datetime.fromisoformat(document["evidence"]["steps"]["latest"][0]["lease_expires_at"])
            == expiry
        )

    async def test_an_admission_wording_renders_a_capacity_wait_on_the_run_row(self, surface):
        run, document = await basic(surface, "queued", reason="capacity_wait")
        assert document["reason"] == document["evidence"]["reason"] == "capacity_wait"
        assert document["evidence"]["steps"]["latest"][0]["state"] == "scheduled"

    async def test_a_stale_verification_reason_names_the_old_and_current_candidates(self, surface):
        run = await seed_run(surface, state="verifying")
        await candidate(surface, run, verification="passed", tested_sha="e" * 40)
        row = (await view(surface, run))["evidence"]["candidates"][0]
        assert row["sha"] == SHA and row["verification"]["tested_sha"] == "e" * 40
        assert not row["verification"]["bound_to_candidate"]

    async def test_a_current_green_verification_needs_no_stale_reason(self, surface):
        run = await seed_run(surface, state="verifying")
        await candidate(surface, run, verification="passed")
        document = await view(surface, run)
        assert document["reason"] is None
        assert document["evidence"]["candidates"][0]["verification"]["bound_to_candidate"]

    async def test_a_healthy_projection_carries_no_reasons(self, surface):
        run, document = await basic(surface)
        assert document["reason"] is None and document["evidence"]["reason"] is None
        assert document["evidence"]["effects"] == []

    async def test_reasons_use_only_the_closed_code_vocabulary(self, surface):
        run = await seed_run(surface)
        response = await surface.client.post(
            "/api/v1/commands",
            headers=headers(),
            json={
                "repository": "project",
                "number": 7,
                "request_id": "bad-word",
                "command": {"kind": "invented-command", "argument": ""},
            },
        )
        assert response.status_code == 422
        assert (await view(surface, run))["evidence"]["commands"]["latest"] == []

    async def test_non_retryable_codes_never_suggest_retry(self, surface):
        run = await seed_run(surface, state="cancelled", cancel_requested=True)
        result = await command(surface, "retry", argument=run.id)
        assert result["status"] == "refused" and result["reason"] == "cancelled"
        assert "start new work" in render(CommandResult.model_validate(result))

    async def test_the_document_render_is_redacted(self, surface):
        run = await seed_run(surface, reason="https://user:private@host.invalid")
        await effect(surface, run, key="Authorization: opaque-private")
        text = (await detail(surface, run)).text
        assert "user:private" not in text and "opaque-private" not in text
        assert "[redacted]" in text


class TestRecoverySurface:
    async def test_the_recorded_candidate_state_outranks_every_fallback(self, surface):
        run = await seed_run(surface, state="failed", reason="collector_completed")
        await candidate(surface, run, state="refused")
        document = await view(surface, run)
        assert document["evidence"]["candidates"][0]["state"] == "refused"
        assert document["evidence"]["candidates"][0]["sha"] is None
        assert document["state"] == "failed"

    async def test_a_failed_driver_exit_derives_a_driver_failed_delivery(self, surface):
        run, document = await basic(surface, "failed", reason="lane_driver_failed")
        assert document["reason"] == document["evidence"]["reason"] == "lane_driver_failed"
        assert document["evidence"]["candidates"] == [] and document["evidence"]["lane"] is None

    async def test_the_blocked_reason_wordings_derive_the_typed_outcomes(self, surface):
        run = await seed_run(surface, state="blocked")
        for reason in ("lane_driver_failed", "candidate_missing", "zero_change"):
            await edit_run(surface, run, reason=reason)
            document = await view(surface, run)
            assert document["reason"] == reason and document["state"] == "blocked"
            assert document["evidence"]["candidates"] == []

    async def test_collected_candidates_derive_delivered_and_nothing_else(self, surface):
        run = await seed_run(surface, state="running")
        await candidate(surface, run, state="received")
        facts = (await view(surface, run))["evidence"]
        assert (
            facts["candidates"][0]["state"] == "received" and facts["candidates"][0]["sha"] is None
        )
        assert (
            facts["candidates"][0]["verification"] is None
            and facts["acceptance"]["state"] == "unobserved"
        )

    async def test_a_failed_delivery_never_renders_as_a_successful_resume(self, surface):
        run = await seed_run(surface, state="blocked", reason="zero_change")
        await command_row(surface, run, kind="resume", state="completed", reason="resume_scheduled")
        document = await view(surface, run)
        assert document["state"] == "blocked" and document["reason"] == "zero_change"
        assert document["evidence"]["candidates"] == []
        assert "resumed" not in document

    async def test_the_ladder_is_five_independent_milestones(self, surface):
        run = await seed_run(surface, state="blocked", reason="lane_paused")
        cp = await checkpoint(surface, run)
        await control(surface, run)
        await command_row(surface, run, kind="resume", state="resolved", reason="resume_scheduled")
        facts = (await view(surface, run))["evidence"]
        assert facts["controls"]["latest"][0]["acknowledged_status"] is None
        assert facts["checkpoints"]["latest"][0]["id"] == cp.id
        assert facts["commands"]["latest"][0]["state"] == "resolved"
        assert facts["lane"] is None and facts["acceptance"]["state"] == "unobserved"

    async def test_an_inconsistent_fence_renders_explicit_uncertainty(self, surface):
        run = await seed_run(surface, state="running")
        cp = await checkpoint(surface, run)
        await control(surface, run, state="acknowledged", checkpoint_id=cp.id)
        await edit_run(surface, run, generation=2, version=2)
        document = await view(surface, run)
        assert document["generation"] == 2
        assert document["evidence"]["checkpoints"]["latest"][0]["generation"] == 1
        assert document["evidence"]["controls"]["latest"][0]["generation"] == 1
        assert document["evidence"]["lane"] is None


class TestDeliveryRoundView:
    async def test_the_five_facts_are_separate_but_linked(self, surface):
        run = await seed_run(surface)
        await candidate(surface, run, verification="passed", review="approve")
        child, row = await round_child(surface, run)
        facts = (await view(surface, run))["evidence"]
        assert facts["rounds"]["latest"][0]["child_run_id"] == child.id
        assert facts["rounds"]["latest"][0]["base_sha"] == facts["candidates"][0]["sha"] == SHA
        assert facts["candidates"][0]["verification"]["status"] == "passed"
        assert facts["candidates"][0]["review"]["decision"] == "approve"
        assert facts["acceptance"]["state"] == "unobserved"

    async def test_after_a_follow_up_round_the_ready_delivery_is_historical(self, surface):
        run = await seed_run(surface)
        await candidate(surface, run, verification="passed")
        child, row = await round_child(surface, run)
        parent_view, child_view = await view(surface, run), await view(surface, child)
        assert parent_view["state"] == "ready_for_human" and child_view["state"] == "queued"
        assert parent_view["evidence"]["rounds"]["latest"][0]["child_run_id"] == child.id
        assert child_view["evidence"]["candidates"] == [] and child_view["parent_run_id"] == run.id

    async def test_a_stale_round_supersedes_nothing(self, surface):
        run = await seed_run(surface)
        await candidate(surface, run, verification="passed")
        _, row = await round_child(surface, run, state="refused")
        document = await view(surface, run)
        assert document["evidence"]["rounds"]["latest"][0]["state"] == "refused"
        assert document["evidence"]["candidates"][0]["sha"] == SHA
        assert document["state"] == "ready_for_human"

    async def test_the_projection_carries_the_round_reference_and_supersession(self, surface):
        run = await seed_run(surface)
        child, row = await round_child(surface, run)
        record = (await view(surface, run))["evidence"]["rounds"]["latest"][0]
        assert (
            record["id"],
            record["root_run_id"],
            record["parent_run_id"],
            record["child_run_id"],
        ) == (row.id, run.id, run.id, child.id)
        assert record["state"] == "open"

    async def test_a_child_run_projects_its_own_round_from_evidence(self, surface):
        run = await seed_run(surface)
        child, row = await round_child(surface, run)
        document = await view(surface, child)
        assert document["subject_id"] == run.subject_id
        assert document["evidence"]["rounds"]["latest"][0]["id"] == row.id
        assert document["parent_run_id"] == document["root_run_id"] == run.id


class TestAcceptanceFact:
    async def test_a_human_merge_is_the_only_acceptance(self, surface):
        run, document = await basic(surface, "ready_for_human", reason="human_merged")
        assert document["reason"] == "human_merged"
        assert document["evidence"]["acceptance"] == {"state": "unobserved"}
        assert "accepted" not in document

    async def test_a_human_close_is_an_honest_rejection(self, surface):
        run, document = await basic(surface, "ready_for_human", reason="human_closed")
        assert document["reason"] == "human_closed"
        assert document["evidence"]["acceptance"] == {"state": "unobserved"}
        assert "accepted" not in document

    async def test_green_ci_alone_is_never_acceptance(self, surface):
        run = await seed_run(surface, state="ready_for_human")
        await candidate(surface, run, verification="passed")
        assert (await view(surface, run))["evidence"]["acceptance"]["state"] == "unobserved"


class TestBudgetBlockedReview:
    async def test_a_refused_amendment_names_the_limiting_axis(self, surface):
        run = await seed_run(surface, state="blocked", reason="closing_budget_exhausted:tokens")
        await pool(surface, run)
        document = await view(surface, run)
        assert document["reason"] == "closing_budget_exhausted:tokens"
        assert document["evidence"]["budgets"]["closing"]["held"]["tokens"] == 100
        assert document["evidence"]["budgets"]["closing"]["held"]["calls"] == 2

    async def test_a_short_reserve_names_the_usd_axis(self, surface):
        run = await seed_run(surface, state="blocked", reason="closing_budget_exhausted:usd")
        await pool(surface, run)
        document = await view(surface, run)
        assert document["reason"].endswith(":usd")
        assert document["evidence"]["budgets"]["closing"]["held"]["usd"] == "2"

    async def test_an_unnamed_axis_is_honest_not_guessed(self, surface):
        run = await seed_run(surface, state="blocked", reason="closing_budget_exhausted")
        await pool(surface, run, observed=Observation(usd=Decimal(".5"), calls=None, tokens=14))
        document = await view(surface, run)
        value = document["evidence"]["budgets"]["closing"]
        assert document["reason"] == "closing_budget_exhausted"
        assert value["unknown_axes"]["calls"] == 1
        assert value["known_accrued_lower_bound"]["tokens"] == 14
        assert value["held"]["tokens"] == 100

    async def test_no_recorded_block_is_none(self, surface):
        run, document = await basic(surface)
        assert document["reason"] is None and document["evidence"]["budgets"] == {}
        assert "budget_blocked_review" not in document

    async def test_the_budget_block_surfaces_as_an_explicit_next_action(self, surface):
        run = await seed_run(surface, state="blocked", reason="closing_budget_exhausted")
        await pool(surface, run)
        document = await view(surface, run)
        assert document["reason"] == "closing_budget_exhausted"
        assert document["evidence"]["budgets"]["closing"]["held"]["usd"] == "2"
        assert "actions" not in document and not surface.gateway.writes


class TestRoundNextActions:
    async def test_the_head_fence_refusals_render_explicitly(self, surface):
        run = await seed_run(surface, state="blocked", reason="branch_drift")
        await candidate(surface, run)
        document = await view(surface, run)
        assert document["reason"] == "branch_drift"
        assert document["evidence"]["candidates"][0]["base_sha"] == BASE
        assert document["evidence"]["acceptance"]["state"] == "unobserved"

    async def test_a_verified_unaccepted_candidate_names_the_pending_human_decision(self, surface):
        run = await seed_run(surface, state="ready_for_human")
        await candidate(surface, run, verification="passed", review="approve")
        document = await view(surface, run)
        assert document["state"] == "ready_for_human"
        assert document["evidence"]["acceptance"]["state"] == "unobserved"


class TestActionSubjectTickets:
    async def test_plan_names_the_expected_candidate_and_round(self, surface):
        run = await seed_run(surface)
        await candidate(surface, run)
        child, row = await round_child(surface, run)
        facts = (await view(surface, run))["evidence"]
        assert facts["candidates"][0]["sha"] == facts["rounds"]["latest"][0]["base_sha"]
        assert facts["rounds"]["latest"][0]["child_run_id"] == child.id

    async def test_every_action_carries_a_one_line_safe_description(self, surface):
        run = await seed_run(surface, state="cancelled", cancel_requested=True)
        result = await command(surface, "retry", argument=run.id)
        body = render(CommandResult.model_validate(result))
        assert (
            body.splitlines()[0] == "An operator revoked this run; start new work with /implement."
        )
        assert "private" not in body

    async def test_decide_refuses_a_moved_candidate_with_the_stale_action_code(self, surface):
        run = await seed_run(surface)
        await closed_hint(
            surface, run, "expected_candidate", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        )

    async def test_decide_refuses_a_moved_round_with_the_stale_action_code(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "expected_round", "old-round")

    async def test_a_matching_subject_ticket_is_allowed(self, surface):
        run = await seed_run(surface)
        result = await command(surface, "status", argument=run.id)
        assert result["status"] == "observed" and result["run_id"] == run.id
        assert json.loads(result["data_json"])["run_version"] == 1

    async def test_the_hint_block_carries_the_subject_ticket_and_safe_action(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "subject_ticket", "client-supplied-ticket")


class TestStatusComment:
    async def test_the_comment_renders_the_five_facts_and_evidence_links(self, surface):
        run = await seed_run(surface)
        await candidate(surface, run, verification="passed", review="approve")
        child, row = await round_child(surface, run)
        result = await command(surface, "status", argument=run.id)
        body = render(CommandResult.model_validate(result))
        assert SHA in body and row.id in body and child.id in body
        assert "unobserved" in body and run.id in body

    async def test_the_identity_is_stable_under_replay_and_moves_with_the_world(self, surface):
        run = await seed_run(surface)
        first = await view(surface, run)
        assert digest(first) == digest(await view(surface, run))
        await candidate(surface, run)
        assert digest(await view(surface, run)) != digest(first)

    async def test_the_identity_digest_is_deterministic_over_the_named_facts(self, surface):
        run = await seed_run(surface)
        document = await view(surface, run)
        assert digest(document) == digest(dict(reversed(list(document.items()))))
        assert document["id"] == run.id

    async def test_with_status_comment_identity_is_replay_stable(self, surface):
        run = await seed_run(surface)
        first = await command(surface, "status", argument=run.id, request_id="same-status")
        await edit_run(surface, run, state="planning", version=2)
        assert await command(surface, "status", argument=run.id, request_id="same-status") == first
        fresh = await command(surface, "status", argument=run.id)
        assert json.loads(first["data_json"])["run_version"] == 1
        assert json.loads(fresh["data_json"])["run_version"] == 2


class TestRoundSurfaceValueFreedom:
    async def test_the_round_surfaces_never_carry_request_text_or_secret_values(self, surface):
        run = await seed_run(surface)
        child, saved = await round_child(surface, run)
        async with surface.service.factory() as session, session.begin():
            row = await session.get(EngineReviewRound, saved.id)
            row.feedback_key = "glpat-private-request"
        await command_row(surface, child, kind="steer", state="completed")
        text = (await detail(surface, run)).text + (await detail(surface, child)).text
        assert (
            "glpat-private" not in text
            and "private request" not in text
            and "feedback_key" not in text
        )

    async def test_requests_surface_even_without_the_rounds_table_section(self, surface):
        run = await seed_run(surface)
        await command_row(surface, run, kind="steer", state="resolved")
        facts = (await view(surface, run))["evidence"]
        assert facts["commands"]["latest"][0]["kind"] == "steer"
        assert facts["rounds"] == {"latest": [], "complete": True}


class TestActionSubjectAndRoundStatusFence:
    async def test_planned_actions_carry_the_full_subject_and_round_status(self, surface):
        run = await seed_run(surface)
        await closed_hint(
            surface, run, "expected_subject", {"provider": "github", "connection": "beta"}
        )

    async def test_the_projection_records_the_current_round_status(self, surface):
        run = await seed_run(surface)
        _, saved = await round_child(surface, run)
        assert (await view(surface, run))["evidence"]["rounds"]["latest"][0]["state"] == "open"
        async with surface.service.factory() as session, session.begin():
            row = await session.get(EngineReviewRound, saved.id)
            row.state = "closed"
        assert (await view(surface, run))["evidence"]["rounds"]["latest"][0]["state"] == "closed"

    async def test_decide_refuses_when_the_round_status_moved_under_the_reference(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "expected_round_status", "open")

    async def test_decide_still_accepts_the_fresh_action_on_the_same_world(self, surface):
        run = await seed_run(surface)
        assert (await command(surface, "cancel", argument=run.id))["reason"] == "cancelled"
        assert (await view(surface, run))["version"] == 2

    async def test_the_audit_fact_carries_the_subject_ticket(self, surface):
        run = await seed_run(surface)
        await command(surface, "status", argument=run.id)
        row = (await model_rows(surface, EngineCommand))[0]
        wire = json.loads(row.document_json)
        assert wire["target"]["repo"]["identity"] == {
            "provider": "gitlab",
            "connection_id": "ce-1",
            "repository_id": "123",
        }
        assert wire["actor"]["native_id"] == "42"

    async def test_a_statusless_action_decides_exactly_as_before(self, surface):
        run = await seed_run(surface)
        result = await command(surface, "status", argument=run.id)
        assert result["status"] == "observed"
        assert json.loads(result["data_json"])["state"] == "queued"


class TestCommandOutcomeFence:
    async def test_the_projection_names_the_newest_unresolved_command(self, surface):
        run = await seed_run(surface)
        await control(surface, run, sequence=1)
        latest = await control(surface, run, sequence=2)
        rows = (await view(surface, run))["evidence"]["controls"]["latest"]
        assert rows[0]["id"] == latest.id and rows[0]["sequence"] == 2
        assert all(row["state"] == "pending" for row in rows)

    async def test_planned_actions_carry_the_command_ticket(self, surface):
        run = await seed_run(surface)
        await closed_hint(surface, run, "expected_command_id", "old-command")

    async def test_the_render_carries_the_fence_subject(self, surface):
        run = await seed_run(surface)
        saved = await command_row(
            surface, run, kind="resume", state="resolved", reason="resume_scheduled"
        )
        facts = (await view(surface, run))["evidence"]
        assert facts["commands"]["latest"][0]["id"] == saved.id
        assert facts["commands"]["latest"][0]["state"] == "resolved"
        assert json.loads(saved.document_json)["target"]["repo"]["identity"] == {
            "provider": "gitlab",
            "connection_id": "ce-1",
            "repository_id": "123",
        }

    async def test_the_refusal_is_the_one_typed_code_under_both_spellings(self, surface):
        run = await seed_run(surface)
        for field in ("stale_action", "staleAction"):
            await closed_hint(surface, run, field, {"command_id": "old"})

    async def test_a_commandless_action_decides_exactly_as_before(self, surface):
        run = await seed_run(surface)
        result = await command(surface, "cancel", argument=run.id)
        assert result["status"] == "accepted"
        assert (await view(surface, run))["cancel_requested"]


@pytest.mark.parametrize("kind", ["status", "why_blocked"])
async def test_outer_status_reason_comes_from_the_same_fresh_snapshot(surface, monkeypatch, kind):
    run = await seed_run(surface)
    reader = surface.service.commands.runs
    original = reader.snapshot

    async def move(selected):
        await edit_run(
            surface, run, state="blocked", reason="Bearer ghp_private123456789", version=2
        )
        return await original(selected)

    monkeypatch.setattr(reader, "snapshot", move)
    result = await command(surface, kind, argument=run.id)
    facts = json.loads(result["data_json"])
    assert result["reason"] == facts["reason"] == "[redacted]"
    assert facts["state"] == "blocked" and facts["run_version"] == 2
    assert "ghp_private" not in json.dumps(result)
