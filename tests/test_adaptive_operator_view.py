"""The persistent operator view (R32-23) — attempts, decisions, recovery.

``derive_state`` is the pure derivation of the closed state vocabulary
from hand-built durable-row fixtures — every state pinned to the row that
proves it, health overlays (wedged / stale / dead) derived, never
asserted. ``apply_update`` is the CAS guard against delayed replays;
``RecoveryActions`` is the state × actor validity matrix whose commands
carry the audit four facts and a version ticket; ``render`` is the
operator document — exact identities, thin evidence, no secrets, no raw
prompts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from forge.adaptive.operator_view import (
    OPERATOR_STATES,
    OperatorAction,
    OperatorProjection,
    RecoveryActions,
    StaleProjectionRejected,
    apply_update,
    derive_state,
    initial_projection,
    render,
)

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "f" * 32


def _run(**over) -> dict:
    row: dict = {
        "id": RUN_ID,
        "status": "planning",
        "base_sha": "b" * 40,
        "candidate_shas": [],
        "plan_digest": "p" * 64,
        "evidence": {},
        "blocked_reason": "",
        "cancel_requested": False,
        "created_at": "2026-09-23T09:00:00+00:00",
        "updated_at": "2026-09-23T09:00:00+00:00",
    }
    row.update(over)
    return row


def _attempt(status: str = "executing", *, attempt_id: str = "att-1", **over) -> dict:
    row: dict = {
        "attempt_id": attempt_id,
        "status": status,
        "started_at": "2026-09-23T11:00:00+00:00",
        "updated_at": "2026-09-23T11:50:00+00:00",
        "generation": 1,
    }
    row.update(over)
    return row


def _cmd(seq: int, kind: str, status: str, **over) -> dict:
    row: dict = {
        "command_id": f"cmd-{seq}",
        "work_id": "wp-1",
        "sequence": seq,
        "kind": kind,
        "status": status,
        "actor_ref": "human:op",
        "created_at": "2026-09-23T11:00:00+00:00",
    }
    row.update(over)
    return row


def _checkpoint(**over) -> dict:
    row: dict = {
        "checkpoint_id": "ck-1",
        "digest": "d" * 64,
        "committed_at": "2026-09-23T11:30:00+00:00",
        "fence": "held",
    }
    row.update(over)
    return row


def _verification(result: str = "passed", **over) -> dict:
    row: dict = {
        "verification_id": "ver-1",
        "result": result,
        "candidate_sha": "c" * 40,
        "at": "2026-09-23T11:45:00+00:00",
    }
    row.update(over)
    return row


def _publication(status: str = "committed", **over) -> dict:
    row: dict = {
        "operation_key": "op-key-1",
        "status": status,
        "operation": "commit",
        "target_ref": "refs/heads/forge/run-1",
        "at": "2026-09-23T11:40:00+00:00",
    }
    row.update(over)
    return row


def _rows(**sections) -> dict:
    rows: dict = {"run": _run()}
    rows.update(sections)
    return rows


# -- the state ladder ----------------------------------------------------------


class TestStateDerivation:
    def test_a_bare_run_row_derives_requested(self):
        derivation = derive_state(_rows(), NOW)

        assert derivation.state == "requested"
        assert derivation.health == ()
        assert derivation.evidence[0]["of"] == "run"

    def test_an_approval_derives_authorized(self):
        derivation = derive_state(
            _rows(approvals=[{"approved_by": "op@corp", "at": "2026-09-23T09:30:00+00:00"}]), NOW
        )

        assert derivation.state == "authorized"
        assert derivation.evidence[0]["of"] == "approval"

    def test_an_executing_attempt_derives_executing(self):
        derivation = derive_state(_rows(attempts=[_attempt()]), NOW)

        assert derivation.state == "executing"
        assert derivation.evidence[0] == {
            "of": "attempt",
            "id": "att-1",
            "ref": derivation.evidence[0]["ref"],
        }
        assert derivation.last_transition_at == "2026-09-23T11:50:00+00:00"

    @pytest.mark.parametrize("rung", ["received", "authorized", "dispatching", "applied"])
    def test_a_commanded_pause_without_a_committed_checkpoint_is_pause_pending(self, rung):
        derivation = derive_state(
            _rows(attempts=[_attempt()], commands=[_cmd(1, "pause", rung)]), NOW
        )

        assert derivation.state == "pause_pending"
        assert derivation.evidence[0]["of"] == "pause command"

    def test_a_committed_checkpoint_with_the_fence_held_is_safely_paused(self):
        derivation = derive_state(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            NOW,
        )

        assert derivation.state == "safely_paused"
        assert [link["of"] for link in derivation.evidence] == ["pause command", "checkpoint"]

    def test_safely_paused_holds_via_the_router_pairing_without_an_explicit_fence_word(self):
        """The router raises the durable fence WITH the pause booking — a
        checkpoint row with no fence word, booked by a checkpointed pause,
        holds the fence by that pairing."""
        derivation = derive_state(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint(fence="")],
            ),
            NOW,
        )

        assert derivation.state == "safely_paused"

    def test_an_explicitly_cleared_fence_is_not_safely_paused(self):
        derivation = derive_state(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint(fence="cleared")],
            ),
            NOW,
        )

        assert derivation.state != "safely_paused"

    def test_an_activated_resume_is_resumed(self):
        derivation = derive_state(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed"), _cmd(2, "resume", "checkpointed")],
                checkpoints=[_checkpoint(activated_at="2026-09-23T11:55:00+00:00")],
            ),
            NOW,
        )

        assert derivation.state == "resumed"
        assert [link["of"] for link in derivation.evidence] == ["resume command", "checkpoint"]

    def test_a_resume_request_alone_is_not_restored(self):
        """R32-23's pinned distinction: a recorded /resume no lane drained,
        or even a drained one whose checkpoint bytes were never ACTIVATED,
        must not display as restored — the run stays safely_paused."""
        derivation = derive_state(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed"), _cmd(2, "resume", "applied")],
                checkpoints=[_checkpoint()],
            ),
            NOW,
        )

        assert derivation.state == "safely_paused"
        assert any("not activated" in reason for reason in derivation.reasons)

    def test_an_undrained_resume_command_stays_safely_paused(self):
        derivation = derive_state(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed"), _cmd(2, "resume", "received")],
                checkpoints=[_checkpoint()],
            ),
            NOW,
        )

        assert derivation.state == "safely_paused"

    def test_a_candidate_without_a_passed_verification_is_unverified(self):
        derivation = derive_state(
            _rows(run=_run(candidate_shas=["c" * 40]), attempts=[_attempt("succeeded")]), NOW
        )

        assert derivation.state == "unverified"

    def test_a_passed_verification_makes_the_candidate_verified_ready(self):
        derivation = derive_state(
            _rows(
                run=_run(candidate_shas=["c" * 40]),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("passed")],
            ),
            NOW,
        )

        assert derivation.state == "verified_ready"
        assert derivation.evidence[0]["of"] == "verification"

    def test_a_failed_verification_does_not_make_the_candidate_ready(self):
        derivation = derive_state(
            _rows(
                run=_run(candidate_shas=["c" * 40]),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("failed")],
            ),
            NOW,
        )

        assert derivation.state == "unverified"

    def test_a_failed_attempt_derives_rejected(self):
        derivation = derive_state(_rows(attempts=[_attempt("failed")]), NOW)

        assert derivation.state == "rejected"

    def test_a_cancelled_run_row_derives_cancelled(self):
        derivation = derive_state(_rows(run=_run(status="cancelled")), NOW)

        assert derivation.state == "cancelled"

    def test_a_failed_run_row_derives_rejected(self):
        derivation = derive_state(_rows(run=_run(status="failed")), NOW)

        assert derivation.state == "rejected"

    def test_a_cancel_request_derives_cancelled_even_before_the_terminal_row(self):
        derivation = derive_state(
            _rows(run=_run(status="committing", cancel_requested=True), attempts=[_attempt()]),
            NOW,
        )

        assert derivation.state == "cancelled"

    def test_an_acceptance_marker_derives_accepted(self):
        derivation = derive_state(
            _rows(
                run=_run(candidate_shas=["c" * 40], evidence={"accepted": True}),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("passed")],
            ),
            NOW,
        )

        assert derivation.state == "accepted"

    def test_the_ladder_orders_pause_above_candidate_states(self):
        """A commanded pause is the headline even when a candidate and its
        verification already exist — control outranks progress."""
        derivation = derive_state(
            _rows(
                run=_run(candidate_shas=["c" * 40]),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("passed")],
                commands=[_cmd(3, "pause", "received")],
            ),
            NOW,
        )

        assert derivation.state == "pause_pending"


class TestWedged:
    def test_executing_with_no_semantic_transition_beyond_the_threshold_is_wedged(self):
        derivation = derive_state(
            _rows(attempts=[_attempt(updated_at="2026-09-23T10:00:00+00:00")]), NOW
        )

        assert derivation.state == "executing"
        assert derivation.display_state == "wedged"
        assert "wedged" in derivation.health

    def test_a_fresh_transition_keeps_it_executing(self):
        derivation = derive_state(
            _rows(attempts=[_attempt(updated_at="2026-09-23T11:50:00+00:00")]), NOW
        )

        assert derivation.display_state == "executing"
        assert derivation.health == ()

    def test_the_threshold_is_a_parameter(self):
        stalled = _rows(attempts=[_attempt(updated_at="2026-09-23T10:00:00+00:00")])

        assert derive_state(stalled, NOW, wedged_after=timedelta(hours=3)).health == ()
        assert derive_state(stalled, NOW, wedged_after=timedelta(minutes=5)).health == ("wedged",)

    def test_wedged_needs_a_readable_clock(self):
        """No parseable timestamp anywhere → no wedged CLAIM: without a
        clock the derivation says executing, never invents an age."""
        derivation = derive_state(
            _rows(attempts=[{"attempt_id": "att-1", "status": "executing"}]), NOW
        )

        assert derivation.state == "executing"
        assert derivation.health == ()

    def test_only_executing_wedges(self):
        derivation = derive_state(
            _rows(
                run=_run(candidate_shas=["c" * 40]),
                attempts=[_attempt("succeeded", updated_at="2026-09-23T08:00:00+00:00")],
            ),
            NOW,
        )

        assert derivation.state == "unverified"
        assert derivation.health == ()

    def test_a_command_transition_resets_the_wedged_clock(self):
        derivation = derive_state(
            _rows(
                attempts=[_attempt(updated_at="2026-09-23T10:00:00+00:00")],
                # the journaled ladder row carries its own ``at`` — the
                # timeline's clock, the same shape the audit journal keeps
                commands=[_cmd(1, "steer", "applied", at="2026-09-23T11:58:00+00:00")],
            ),
            NOW,
        )

        assert derivation.display_state == "executing"


class TestStaleAndDead:
    def test_a_stored_projection_older_than_the_rows_derives_stale(self):
        stored = initial_projection(_rows(attempts=[_attempt()]), NOW)
        newer = _rows(
            attempts=[_attempt()],
            commands=[_cmd(1, "pause", "received", created_at="2026-09-23T11:59:00+00:00")],
        )

        derivation = derive_state(newer, NOW, stored=stored)

        assert derivation.display_state == "stale"
        assert derivation.state == "pause_pending"  # the underlying state stays visible
        assert "stale" in derivation.health

    def test_a_fresh_stored_projection_derives_no_stale(self):
        rows = _rows(attempts=[_attempt()])
        stored = initial_projection(rows, NOW)

        assert derive_state(rows, NOW, stored=stored).health == ()

    def test_a_failed_run_with_unresolved_effects_is_dead(self):
        derivation = derive_state(
            _rows(
                run=_run(status="failed"),
                attempts=[_attempt("failed")],
                publications=[_publication("dispatched"), _publication("unknown")],
            ),
            NOW,
        )

        assert derivation.state == "rejected"
        assert derivation.display_state == "dead"
        assert "dead" in derivation.health

    def test_a_failed_run_with_resolved_effects_stays_rejected(self):
        derivation = derive_state(
            _rows(run=_run(status="failed"), publications=[_publication("committed")]), NOW
        )

        assert derivation.display_state == "rejected"
        assert derivation.health == ()

    def test_dead_evidence_names_the_unreconciled_effects(self):
        derivation = derive_state(
            _rows(run=_run(status="cancelled"), publications=[_publication("requested")]), NOW
        )

        assert derivation.display_state == "dead"
        assert any("unresolved external effect" in reason for reason in derivation.reasons)


class TestEvidenceLinks:
    def test_every_derivation_carries_row_digest_evidence(self):
        derivation = derive_state(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            NOW,
        )

        assert derivation.evidence
        assert all(link["ref"].startswith("sha256:") for link in derivation.evidence)
        assert all(link["id"] for link in derivation.evidence)

    def test_every_state_of_the_vocabulary_derives(self):
        fixtures = {
            "requested": _rows(),
            "authorized": _rows(
                approvals=[{"approved_by": "op", "at": "2026-09-23T09:30:00+00:00"}]
            ),
            "executing": _rows(attempts=[_attempt()]),
            "pause_pending": _rows(attempts=[_attempt()], commands=[_cmd(1, "pause", "applied")]),
            "safely_paused": _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            "resumed": _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "resume", "applied")],
                checkpoints=[_checkpoint(activated_at="2026-09-23T11:55:00+00:00")],
            ),
            "unverified": _rows(
                run=_run(candidate_shas=["c" * 40]), attempts=[_attempt("succeeded")]
            ),
            "verified_ready": _rows(
                run=_run(candidate_shas=["c" * 40]),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("passed")],
            ),
            "rejected": _rows(attempts=[_attempt("failed")]),
            "cancelled": _rows(run=_run(status="cancelled")),
            "accepted": _rows(
                run=_run(evidence={"accepted": True}), attempts=[_attempt("accepted")]
            ),
        }
        for expected, rows in fixtures.items():
            assert derive_state(rows, NOW).state == expected, expected
            assert derive_state(rows, NOW).evidence, expected

    def test_a_projection_without_a_run_row_is_nothing(self):
        with pytest.raises(ValueError, match="no run row"):
            derive_state({"attempts": [_attempt()]}, NOW)


# -- the versioned projection and the CAS guard --------------------------------


class TestProjectionAndCas:
    def test_the_initial_projection_is_version_one(self):
        projection = initial_projection(_rows(attempts=[_attempt()]), NOW)

        assert projection.projection_version == 1
        assert projection.state == "executing"
        assert projection.source_digest

    def test_apply_update_mints_the_next_version(self):
        projection = initial_projection(_rows(attempts=[_attempt()]), NOW)
        updated = apply_update(
            projection,
            _rows(attempts=[_attempt()], commands=[_cmd(1, "pause", "received")]),
            projection.projection_version,
            now=NOW,
        )

        assert updated.projection_version == 2
        assert updated.state == "pause_pending"

    def test_a_delayed_replay_is_rejected_and_the_newer_projection_kept(self):
        projection = initial_projection(_rows(attempts=[_attempt()]), NOW)  # v1
        v2 = apply_update(
            projection,
            _rows(attempts=[_attempt()], commands=[_cmd(1, "pause", "checkpointed")]),
            expected_version=1,
            now=NOW,
        )
        v3 = apply_update(
            v2,
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            expected_version=2,
            now=NOW,
        )
        assert v3.projection_version == 3

        # the delayed update, computed against v1 rows, must NOT overwrite v3
        with pytest.raises(StaleProjectionRejected) as excinfo:
            apply_update(
                v3,
                _rows(attempts=[_attempt()], commands=[_cmd(1, "pause", "received")]),
                expected_version=1,
                now=NOW,
            )
        assert excinfo.value.expected_version == 1
        assert excinfo.value.current_version == 3
        assert excinfo.value.current_state == "safely_paused"
        assert excinfo.value.safe_next_action == "probe"  # the always-safe re-read

        # the newer projection is kept: the next legitimate update builds on v3
        v4 = apply_update(
            v3,
            _rows(
                run=_run(status="failed"),
                attempts=[_attempt("failed")],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            expected_version=3,
            now=NOW,
        )
        assert v4.projection_version == 4

    def test_an_update_ahead_of_the_stored_version_is_a_bug(self):
        projection = initial_projection(_rows(), NOW)

        with pytest.raises(ValueError, match="monotonic"):
            apply_update(projection, _rows(), expected_version=7, now=NOW)

    def test_versions_are_strictly_monotonic_across_a_chain(self):
        projection = initial_projection(_rows(), NOW)
        versions = [projection.projection_version]
        rows = _rows(attempts=[_attempt()])
        for expected in range(1, 4):
            projection = apply_update(projection, rows, expected_version=expected, now=NOW)
            versions.append(projection.projection_version)
        assert versions == [1, 2, 3, 4]

    def test_an_update_cannot_switch_runs(self):
        projection = initial_projection(_rows(), NOW)
        other = _rows()
        other["run"] = _run(id="e" * 32)

        with pytest.raises(ValueError, match="cannot replace"):
            apply_update(projection, other, expected_version=1, now=NOW)

    def test_the_stale_rejection_names_the_current_state_and_safe_next_action(self):
        projection = initial_projection(_rows(attempts=[_attempt()]), NOW)
        v2 = apply_update(
            projection,
            _rows(attempts=[_attempt()], commands=[_cmd(1, "pause", "received")]),
            expected_version=1,
            now=NOW,
        )
        assert v2.state == "pause_pending"

        # the v1-computed update replays against v2 — refused, current state named
        with pytest.raises(StaleProjectionRejected) as excinfo:
            apply_update(v2, _rows(), expected_version=1, now=NOW)

        assert excinfo.value.expected_version == 1
        assert excinfo.value.current_version == 2
        assert excinfo.value.current_state == "pause_pending"
        assert excinfo.value.safe_next_action == "probe"  # the always-safe re-read
        assert "pause_pending" in str(excinfo.value)


# -- the recovery-action validity matrix ---------------------------------------


class TestRecoveryActionsMatrix:
    def test_resume_is_valid_only_in_safely_paused(self):
        for state in OPERATOR_STATES:
            actions = RecoveryActions.valid_for(state, "approver")
            assert ("resume" in actions) == (state == "safely_paused"), state

    def test_steer_is_valid_only_while_executing(self):
        for state in OPERATOR_STATES:
            actions = RecoveryActions.valid_for(state, "approver")
            assert ("steer" in actions) == (state == "executing"), state

    def test_retry_is_valid_only_in_failed_terminals(self):
        for state in OPERATOR_STATES:
            actions = RecoveryActions.valid_for(state, "approver")
            assert ("retry" in actions) == (state in ("rejected", "dead")), state

    def test_a_stale_view_allows_only_the_probe(self):
        assert RecoveryActions.valid_for("stale", "approver") == ("probe",)

    def test_an_accepted_run_needs_nothing(self):
        assert RecoveryActions.valid_for("accepted", "approver") == ()

    def test_observers_and_automation_may_only_probe(self):
        for state in OPERATOR_STATES:
            assert set(RecoveryActions.valid_for(state, "observer")) <= {"probe"}
            assert set(RecoveryActions.valid_for(state, "automation")) <= {"probe"}

    def test_unknown_states_and_roles_fail_visibly(self):
        with pytest.raises(ValueError, match="unknown operator state"):
            RecoveryActions.valid_for("teleported", "approver")
        with pytest.raises(ValueError, match="unknown actor role"):
            RecoveryActions.valid_for("executing", "intern")

    def test_every_state_offers_the_probe_to_approvers(self):
        for state in OPERATOR_STATES:
            if state == "accepted":
                continue  # done is done — nothing pending, not even a probe
            assert "probe" in RecoveryActions.valid_for(state, "approver"), state


class TestRecoveryActionPlanning:
    def _paused(self) -> OperatorProjection:
        return initial_projection(
            _rows(
                run=_run(candidate_shas=["c" * 40]),
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            NOW,
        )

    def test_plan_carries_the_four_facts_and_the_cas_ticket(self):
        projection = self._paused()

        actions = RecoveryActions.plan(projection, "op@corp", "approver", at=NOW)

        assert [a.action for a in actions] == ["resume", "cancel", "probe"]
        resume = actions[0]
        fact = resume.audit_fact()
        assert fact["who"] == "op@corp"  # who
        assert fact["digest"] == "c" * 40  # what exactly — the candidate sha, never "latest"
        assert fact["when"] == NOW.astimezone(timezone.utc).isoformat()  # when
        assert RUN_ID in fact["linkage"] and "v1" in fact["linkage"]  # why / linkage
        assert resume.expected_version == 1
        assert resume.via == "command_router:/resume"  # links to the guarded path

    def test_the_action_digest_names_the_exact_artifact_in_priority_order(self):
        paused = self._paused()
        assert paused.action_digest == "c" * 40  # candidate sha first

        no_candidate = initial_projection(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            NOW,
        )
        assert no_candidate.action_digest == "d" * 64  # then the checkpoint digest

        bare = initial_projection(_rows(), NOW)
        assert bare.action_digest == "p" * 64  # then the plan digest

    def test_plan_refuses_unknown_roles(self):
        with pytest.raises(ValueError, match="unknown actor role"):
            RecoveryActions.plan(self._paused(), "x", "intern")


class TestRecoveryActionDecisions:
    def _paused_v1(self) -> OperatorProjection:
        return initial_projection(
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            NOW,
        )

    def test_a_fresh_matching_action_is_allowed(self):
        projection = self._paused_v1()
        (resume,) = [
            a
            for a in RecoveryActions.plan(projection, "op@corp", "approver", at=NOW)
            if a.action == "resume"
        ]

        decision = RecoveryActions.decide(resume, projection)

        assert decision.allowed
        assert decision.current_state == "safely_paused"

    def test_a_stale_command_from_an_old_status_comment_is_refused_with_the_current_state(self):
        """The pinned scenario: a pause planned against v1 (the old status
        comment, the run executing) reaches the console after the world
        moved on to a safely held pause at v3 — refused, with the CURRENT
        state and the safe next action named."""
        v1 = initial_projection(_rows(attempts=[_attempt()]), NOW)
        assert v1.state == "executing"
        (pause,) = [
            a
            for a in RecoveryActions.plan(v1, "op@corp", "approver", at=NOW)
            if a.action == "pause"
        ]
        v2 = apply_update(
            v1,
            _rows(attempts=[_attempt()], commands=[_cmd(1, "pause", "received")]),
            expected_version=1,
            now=NOW,
        )
        current = apply_update(
            v2,
            _rows(
                attempts=[_attempt()],
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            expected_version=2,
            now=NOW,
        )
        assert current.state == "safely_paused"

        decision = RecoveryActions.decide(pause, current)

        assert decision.allowed is False
        assert "stale command" in decision.reason
        assert decision.current_state == "safely_paused"
        assert decision.safe_next_action == "resume"  # the safe next action when safely paused

    def test_a_command_ahead_of_the_stored_version_is_refused(self):
        projection = self._paused_v1()
        ahead = OperatorAction(
            action="resume",
            state="safely_paused",
            actor_role="approver",
            actor="op@corp",
            digest=projection.action_digest,
            at=NOW.isoformat(),
            linkage=f"run:{RUN_ID}",
            expected_version=projection.projection_version + 5,
            via="command_router:/resume",
        )

        decision = RecoveryActions.decide(ahead, projection)

        assert decision.allowed is False
        assert "ahead of the stored projection" in decision.reason
        assert decision.current_state == "safely_paused"

    def test_a_state_changed_command_is_refused_with_the_safe_next_action(self):
        v1 = self._paused_v1()
        (resume,) = [
            a
            for a in RecoveryActions.plan(v1, "op@corp", "approver", at=NOW)
            if a.action == "resume"
        ]
        current = apply_update(
            v1,
            _rows(run=_run(status="failed"), attempts=[_attempt("failed")]),
            expected_version=1,
            now=NOW,
        )
        assert current.state == "rejected"

        decision = RecoveryActions.decide(resume, current)

        assert decision.allowed is False
        assert decision.current_state == "rejected"
        assert decision.safe_next_action == "retry"

    def test_an_observer_pause_is_refused(self):
        projection = self._paused_v1()
        observer_pause = OperatorAction(
            action="pause",
            state="safely_paused",
            actor_role="observer",
            actor="watcher@corp",
            digest=projection.action_digest,
            at=NOW.isoformat(),
            linkage=f"run:{RUN_ID}",
            expected_version=projection.projection_version,
            via="command_router:/pause",
        )

        decision = RecoveryActions.decide(observer_pause, projection)

        assert decision.allowed is False
        assert "observer" in decision.reason
        assert decision.safe_next_action == "probe"


# -- the render -----------------------------------------------------------------


class TestRender:
    def _full_rows(self) -> dict:
        return _rows(
            run=_run(candidate_shas=["c" * 40]),
            attempts=[
                _attempt("failed", attempt_id="att-1", generation=2),
                _attempt("succeeded", attempt_id="att-2", generation=3),
            ],
            commands=[_cmd(1, "pause", "checkpointed")],
            checkpoints=[_checkpoint(checkpoint_id="ck-9", digest="e" * 64)],
            verifications=[_verification("passed")],
        )

    def test_render_carries_every_exact_identity(self):
        projection = initial_projection(self._full_rows(), NOW)

        document = render(projection)

        assert document["schema"] == "forge.operator.view/1"
        assert document["identity"] == {
            "source_sha": "b" * 40,
            "candidate_shas": ["c" * 40],
            "checkpoint_id": "ck-9",
            "checkpoint_digest": "e" * 64,
            "plan_digest": "p" * 64,
            "attempt_id": "att-2",
            "generation": 3,
        }
        assert document["run_id"] == RUN_ID
        assert document["projection_version"] == 1

    def test_render_never_leaks_secret_values(self):
        rows = _rows(
            run=_run(
                base_sha="sk-abcdefghijklmn",
                evidence={"api_key": "ghp_abcdefghijklmn", "note": "Bearer abcdef12345678"},
            ),
            publications=[_publication(status="dispatched", target_ref="glpat-xyz987654321")],
        )
        projection = initial_projection(rows, NOW)

        rendered = json.dumps(render(projection))

        assert "sk-abcdefghijklmn" not in rendered
        assert "ghp_abcdefghijklmn" not in rendered
        assert "Bearer abcdef12345678" not in rendered
        assert "glpat-xyz987654321" not in rendered
        assert "[redacted]" in rendered

    def test_render_never_carries_raw_prompt_text(self):
        rows = _rows(
            run=_run(evidence={"prompt": "Rewrite the payment module and keep the fixtures"}),
            attempts=[_attempt()],
            commands=[_cmd(1, "steer", "applied", payload={"text": "tighten the retry bounds"})],
        )
        projection = initial_projection(rows, NOW)

        rendered = json.dumps(render(projection))

        assert "tighten the retry bounds" not in rendered
        assert "Rewrite the payment module" not in rendered

    def test_render_lists_unresolved_external_effects(self):
        rows = _rows(
            run=_run(status="failed"),
            attempts=[_attempt("failed")],
            publications=[
                _publication("dispatched"),
                _publication("unknown"),
                _publication("committed"),
            ],
        )
        projection = initial_projection(rows, NOW)

        document = render(projection)

        assert document["state"] == "dead"
        assert [effect["status"] for effect in document["unresolved_effects"]] == [
            "dispatched",
            "unknown",
        ]

    def test_render_reuses_the_status_projection_line(self):
        rows = _rows(
            run=_run(blocked_reason=""),
            questions=[{"question_id": "q-1", "resolved": False}],
        )
        projection = initial_projection(rows, NOW)

        document = render(projection)

        assert document["waiting_on"] == "question"
        assert "Answer 1 open question" in document["summary"]

    def test_render_carries_blocked_reason_and_thin_evidence_links(self):
        rows = _rows(run=_run(blocked_reason="waiting on vendor quota"))
        projection = initial_projection(rows, NOW)

        document = render(projection)

        assert document["blocked_reason"] == "waiting on vendor quota"
        assert document["evidence"]
        assert all(
            set(link) == {"of", "id", "ref"} and link["ref"].startswith("sha256:")
            for link in document["evidence"]
        )

    def test_render_keeps_unobserved_sources_explicit(self):
        projection = initial_projection(_rows(attempts=[_attempt()]), NOW)

        document = render(projection)

        assert document["rows_observed"]["attempts"]  # observed, with a stamp
        assert document["rows_observed"]["checkpoints"] == ""  # never observed
        assert document["rows_observed"]["run"]
