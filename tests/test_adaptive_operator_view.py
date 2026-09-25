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
    BLOCKED_CODES,
    NON_RETRYABLE_CODES,
    OPERATOR_STATES,
    RECOVERY_MILESTONES,
    OperatorAction,
    OperatorProjection,
    RecoveryActions,
    StaleProjectionRejected,
    apply_update,
    delivery_outcome_of,
    derive_state,
    explain_blocked,
    initial_projection,
    recovery_document,
    recovery_ladder,
    render,
    status_note_lines,
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

    # -- R37-03: the CURRENT candidate binds readiness; older passes are
    # -- history, never current readiness.

    def test_a_pass_for_an_earlier_candidate_is_history_not_readiness(self):
        """Candidate history [A, B] with a pass only for A: B is the
        CURRENT candidate (the last member) and reads unverified; A's
        pass renders as a historical_pass, never as current readiness."""
        a, b = "a" * 40, "b" * 40
        projection = initial_projection(
            _rows(
                run=_run(candidate_shas=[a, b]),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("passed", candidate_sha=a)],
            ),
            NOW,
        )

        assert projection.state == "unverified"
        assert projection.identity["active_candidate"] == b
        assert projection.action_digest == b  # the headline names the CURRENT one
        assert [entry["verdict"] for entry in projection.verification_history] == [
            "historical_pass"
        ]
        assert projection.verification_history[0]["candidate_sha"] == a

    def test_a_pass_for_the_last_member_is_current_readiness(self):
        a, b = "a" * 40, "b" * 40
        derivation = derive_state(
            _rows(
                run=_run(candidate_shas=[a, b]),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("passed", candidate_sha=b)],
            ),
            NOW,
        )

        assert derivation.state == "verified_ready"

    def test_an_explicit_active_candidate_pointer_wins_over_the_list_order(self):
        """The run row's ``active_candidate_sha`` pointer names the current
        candidate even when it is not the last list member — and a pass
        for the LAST member is then the historical one."""
        a, b = "a" * 40, "b" * 40
        projection = initial_projection(
            _rows(
                run=_run(candidate_shas=[a, b], active_candidate_sha=a),
                attempts=[_attempt("succeeded")],
                verifications=[_verification("passed", candidate_sha=b)],
            ),
            NOW,
        )

        assert projection.identity["active_candidate"] == a
        assert projection.state == "unverified"
        assert projection.verification_history[0]["candidate_sha"] == b

    def test_the_candidate_history_renders_beside_the_current_one(self):
        a, b = "a" * 40, "b" * 40
        projection = initial_projection(
            _rows(run=_run(candidate_shas=[a, b]), attempts=[_attempt("succeeded")]), NOW
        )
        document = render(projection)

        assert document["identity"]["candidate_shas"] == [a, b]
        assert document["identity"]["active_candidate"] == b
        assert "1 earlier candidate" in "\n".join(status_note_lines(projection))

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
            "active_candidate": "c" * 40,  # R37-03: the CURRENT candidate
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


# -- the typed blocked-reason explanations (R37-16) ---------------------------


class TestExplainBlocked:
    """``explain_blocked`` — the typed, evidence-linked diagnostics: each
    observed condition renders its outcome CODE, a one-line explanation,
    a thin link to the EXACT proving row and the SAFE next action; the
    non-retryable conditions never suggest retry."""

    def test_a_revoked_authority_is_never_answered_with_retry(self):
        rows = _rows(run=_run(blocked_reason="provider credential revoked (401 unauthorized)"))
        projection = initial_projection(rows, NOW)

        reasons = explain_blocked(projection)
        revoked = next(reason for reason in reasons if reason.code == "revoked_authority")

        assert revoked.retryable is False
        assert revoked.suggested_action == "rotate_or_rebind_credential"
        assert revoked.via == "runbook:token-rotation"
        assert "retry" not in revoked.suggested_action  # the headline pin
        assert revoked.explanation.lower().startswith("the run is blocked")
        assert revoked.evidence["of"] == "run"
        assert revoked.evidence["id"] == RUN_ID
        assert revoked.evidence["ref"].startswith("sha256:")

    def test_the_revocation_vocabulary_covers_the_provider_refusals(self):
        for reason_text in (
            "token expired",
            "permission denied on the project",
            "forbidden: the app was suspended",
            "webhook secret unauthorized",
        ):
            rows = _rows(run=_run(blocked_reason=reason_text))
            projection = initial_projection(rows, NOW)
            assert any(r.code == "revoked_authority" for r in explain_blocked(projection)), (
                reason_text
            )

    def test_a_plain_blockage_is_not_a_revocation(self):
        rows = _rows(run=_run(blocked_reason="waiting on the CI lane"))
        projection = initial_projection(rows, NOW)

        assert not any(reason.code == "revoked_authority" for reason in explain_blocked(projection))

    def test_each_uncertain_effect_renders_its_own_reason_with_its_row_link(self):
        rows = _rows(
            run=_run(status="failed"),
            publications=[
                _publication(status="dispatched", operation_key="op-inflight"),
                _publication(status="unknown", operation_key="op-lost"),
                _publication(status="committed", operation_key="op-done"),
            ],
        )
        projection = initial_projection(rows, NOW)

        uncertain = [
            reason
            for reason in explain_blocked(projection)
            if reason.code == "uncertain_native_effect"
        ]

        assert [reason.evidence["id"] for reason in uncertain] == ["op-inflight", "op-lost"]
        for reason in uncertain:
            assert reason.retryable is False
            assert reason.suggested_action == "reconcile"
            assert reason.evidence["of"] == "publication"
            assert reason.evidence["ref"].startswith("sha256:")
            assert "unproven" in reason.explanation

    def test_a_lost_required_checkpoint_names_restore_not_retry(self):
        """A held pause fence whose checkpoint authority reads missing: the
        resume path is gone — restore or retire, never retry."""
        rows = _rows(
            run=_run(status="proposing"),
            commands=[_cmd(1, "pause", "checkpointed")],
            checkpoints=[_checkpoint(checkpoint_id="", digest="", fence="held")],
        )
        projection = initial_projection(rows, NOW)

        reasons = explain_blocked(
            projection, coverage={"checkpoints": "missing"}, checkpoints=rows["checkpoints"]
        )
        loss = next(reason for reason in reasons if reason.code == "required_checkpoint_loss")

        assert loss.retryable is False
        assert loss.suggested_action == "restore_checkpoint_or_retire"
        assert loss.via == "runbook:backup-restore"
        assert "bytes to restore" in loss.explanation
        assert loss.evidence["of"] == "checkpoint"

    def test_a_present_checkpoint_authority_is_not_a_loss(self):
        rows = _rows(
            run=_run(status="proposing"),
            commands=[_cmd(1, "pause", "checkpointed")],
            checkpoints=[_checkpoint(fence="held")],
        )
        projection = initial_projection(rows, NOW)

        assert not any(
            reason.code == "required_checkpoint_loss"
            for reason in explain_blocked(
                projection, coverage={"checkpoints": "present"}, checkpoints=rows["checkpoints"]
            )
        )

    def test_an_uncertain_lease_renders_a_capacity_wait_with_its_age(self):
        rows = _rows(run=_run(status="proposing"))
        projection = initial_projection(rows, NOW)
        occupancy = [
            {
                "lease_id": "lease-1",
                "occupancy": "dispatched_unknown",
                "acquired_at": (NOW - timedelta(hours=2)).isoformat(),
                "released_at": "",
            },
            {
                "lease_id": "lease-2",
                "occupancy": "observed_terminal",
                "acquired_at": (NOW - timedelta(hours=3)).isoformat(),
                "released_at": (NOW - timedelta(minutes=1)).isoformat(),
            },
        ]

        reasons = explain_blocked(projection, occupancy=occupancy)
        waits = [reason for reason in reasons if reason.code == "capacity_wait"]

        assert len(waits) == 1  # the released lease is not a wait
        wait = waits[0]
        assert wait.evidence == {
            "of": "lease",
            "id": "lease-1",
            "ref": wait.evidence["ref"],
        }
        assert "dispatched_unknown" in wait.explanation
        assert "7200s" in wait.explanation  # the age of the uncertain hold
        assert wait.retryable is True

    def test_an_admission_wording_renders_a_capacity_wait_on_the_run_row(self):
        rows = _rows(run=_run(blocked_reason="queued: project at capacity (queue full)"))
        projection = initial_projection(rows, NOW)

        wait = next(
            reason for reason in explain_blocked(projection) if reason.code == "capacity_wait"
        )

        assert wait.evidence["of"] == "run"
        assert "capacity" in wait.explanation
        assert wait.suggested_action == "wait_for_reconciler"

    def test_a_stale_verification_reason_names_the_old_and_current_candidates(self):
        old, current = "a" * 40, "b" * 40
        rows = _rows(
            run=_run(status="waiting_ci", candidate_shas=[old, current]),
            verifications=[_verification(candidate_sha=old)],
        )
        projection = initial_projection(rows, NOW)

        stale = next(
            reason for reason in explain_blocked(projection) if reason.code == "verification_stale"
        )

        assert old[:12] in stale.explanation
        assert current[:12] in stale.explanation
        assert stale.evidence["of"] == "verification"
        assert stale.evidence["id"] == "ver-1"
        assert stale.suggested_action == "verify_current_candidate"
        assert stale.retryable is True

    def test_a_current_green_verification_needs_no_stale_reason(self):
        rows = _rows(
            run=_run(status="waiting_ci", candidate_shas=["c" * 40]),
            verifications=[_verification(candidate_sha="c" * 40)],
        )
        projection = initial_projection(rows, NOW)

        assert not any(
            reason.code == "verification_stale" for reason in explain_blocked(projection)
        )

    def test_a_healthy_projection_carries_no_reasons(self):
        projection = initial_projection(_rows(), NOW)

        assert explain_blocked(projection) == []

    def test_reasons_use_only_the_closed_code_vocabulary(self):
        rows = _rows(
            run=_run(status="failed", blocked_reason="credential revoked"),
            publications=[_publication(status="dispatched")],
        )
        projection = initial_projection(rows, NOW)

        codes = {reason.code for reason in explain_blocked(projection)}

        assert codes <= set(BLOCKED_CODES)
        assert {"revoked_authority", "uncertain_native_effect"} <= codes

    def test_non_retryable_codes_never_suggest_retry(self):
        rows = _rows(
            run=_run(status="failed", blocked_reason="revoked"),
            publications=[_publication(status="unknown")],
        )
        projection = initial_projection(rows, NOW)

        for reason in explain_blocked(projection):
            if reason.code in NON_RETRYABLE_CODES:
                assert reason.retryable is False
                assert "retry" not in reason.suggested_action

    def test_the_document_render_is_redacted(self):
        rows = _rows(run=_run(blocked_reason="forbidden: Bearer ghp_aaaaaaaaaaaaaaaaaaaa"))
        projection = initial_projection(rows, NOW)

        document = next(
            reason for reason in explain_blocked(projection) if reason.code == "revoked_authority"
        ).as_document()

        assert set(document) == {
            "code",
            "explanation",
            "evidence",
            "suggested_action",
            "via",
            "retryable",
        }
        assert "ghp_aaaaaaaaaaaaaaaaaaaa" not in json.dumps(document)


class TestRecoverySurface:
    """The R38-15 recovery surface (pure arms): the delivery outcome's
    derivation priority, the never-a-successful-resume display rule and
    the five-milestone ladder's independence on hand-built rows."""

    def test_the_recorded_candidate_state_outranks_every_fallback(self):
        # the marker wins even when the driver exit and older candidates
        # could suggest a different story
        rows = _rows(
            run=_run(
                candidate_shas=["c" * 40],
                blocked_reason="waiting on the CI lane",
                evidence={
                    "harness": {"driver_exit": "completed", "candidate_state": "zero_change"}
                },
            )
        )

        delivery = delivery_outcome_of(rows)

        assert delivery.outcome == "empty_diff_no_effect"
        assert delivery.failed is True
        assert delivery.reason == "harness candidate_state=zero_change"
        assert delivery.evidence["of"] == "run"

    def test_a_failed_driver_exit_derives_a_driver_failed_delivery(self):
        rows = _rows(run=_run(evidence={"harness": {"driver_exit": "failed"}}))

        assert delivery_outcome_of(rows).outcome == "driver_failed"

    def test_the_blocked_reason_wordings_derive_the_typed_outcomes(self):
        cases = {
            "repair_no_effect: empty diff": "empty_diff_no_effect",
            "harness_no_changes": "empty_diff_no_effect",
            "harness_artifact_missing": "collection_failed",
            "harness_candidate_invalid: bad diff": "collection_failed",
            "harness_driver_failed (exit=failed)": "driver_failed",
            "waiting on the CI lane": "not_collected_yet",  # no delivery wording
        }
        for reason, expected in cases.items():
            rows = _rows(run=_run(blocked_reason=reason))
            assert delivery_outcome_of(rows).outcome == expected, reason

    def test_collected_candidates_derive_delivered_and_nothing_else(self):
        rows = _rows(run=_run(candidate_shas=["c" * 40]))

        delivery = delivery_outcome_of(rows)

        assert delivery.outcome == "delivered"
        assert delivery.failed is False

    def test_a_failed_delivery_never_renders_as_a_successful_resume(self):
        rows = _rows(
            run=_run(status="proposing", evidence={"harness": {"candidate_state": "zero_change"}}),
            commands=[_cmd(1, "pause", "checkpointed"), _cmd(2, "resume", "applied")],
            checkpoints=[
                _checkpoint(
                    activated_at="2026-09-23T11:50:00+00:00",
                    activation="matched",
                    fence="cleared",
                )
            ],
        )
        projection = initial_projection(rows, NOW)
        assert projection.state == "resumed"  # the state ladder is honest

        document = recovery_document(rows, state=projection.state, now=NOW)

        assert document["schema"] == "forge.operator.recovery/1"
        assert document["delivery"]["outcome"] == "empty_diff_no_effect"
        assert document["delivery"]["failed"] is True
        assert "FAILED/no-effect delivery" in document["delivery"]["headline"]
        assert document["hint"]["advisory"], "a failed delivery always carries guidance"

    def test_the_ladder_is_five_independent_milestones(self):
        ladder = recovery_ladder(
            _rows(
                commands=[_cmd(1, "pause", "checkpointed")],
                checkpoints=[_checkpoint()],
            ),
            coverage={"commands": "present", "checkpoints": "present", "occupancy": "present"},
            occupancy=[{"lease_id": "lease-1", "released_at": ""}],  # still open
        )

        assert set(ladder) == set(RECOVERY_MILESTONES)
        assert ladder["pause_requested"]["status"] == "present"
        assert ladder["checkpoint_committed"]["status"] == "present"
        assert ladder["runner_stopped"]["status"] == "absent"
        assert ladder["resume_authorized"]["status"] == "absent"
        assert ladder["exact_resume_applied"]["status"] == "absent"

    def test_an_inconsistent_fence_renders_explicit_uncertainty(self):
        document = recovery_document(_rows(), projection_inconsistent=True)

        assert document["consistency"] == "inconsistent"
        assert "re-read before acting" in document["uncertainty"]
