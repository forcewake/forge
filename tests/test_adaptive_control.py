"""The CTL epic core: mailbox, pause ordering, resume epochs, bounded steering, cancel.

Each test pins one invariant the review (05868e9, CTL-04..CTL-08) states as
the DIFFERENCE between durable control and a UI status change: redelivery
must not buy a second approval, a wrong-epoch command expires instead of
applying, a pause is persisted before the interrupt, a timeout reports the
last recoverable checkpoint rather than a clean pause, steering never
grants authority, and a final cancel correlates accepted effects instead
of claiming them undone.
"""

from __future__ import annotations

import hashlib

import pytest

from forge.adaptive.control import (
    Mailbox,
    MailboxSurface,
    PauseState,
    cancel_generation_applies,
    classify_instruction,
    deliver_steer,
    drain_turn,
    final_cancel,
    late_effect_outcome,
    new_execution_epoch,
    new_publication_epoch,
    promote_to_proposal,
    recorded_pause,
    request_pause,
    resume_check,
    send_interrupt,
)
from forge.adaptive.models import ControlCommand
from forge.adaptive.wiring import OperatorControlService

SCOPES = {
    "server_authenticated_human": ("human:reviewer-17",),
    "operator_token": ("token:ops-1",),
    "automation_reconciler": ("svc:reconciler",),
}


def _command(**overrides) -> ControlCommand:
    base = {
        "schema": "forge.proposal.control-command/1",
        "command_id": "cmd-1",
        "work_id": "wp-demo-1",
        "sequence": 1,
        "kind": "pause",
        "actor_ref": "human:reviewer-17",
        "actor_origin": "server_authenticated_human",
        "idempotency_key": "gitlab-note:1",
        "status": "received",
    }
    base.update(overrides)
    return ControlCommand.model_validate(base)


def _paused_work_state(**overrides) -> PauseState:
    base = {
        "work_id": "wp-demo-1",
        "publication_epoch": 1,
        "last_applied_command_sequence": 3,
    }
    base.update(overrides)
    return PauseState(**base)


class TestMailboxLadder:
    def test_submit_starts_the_ladder_at_received(self):
        mailbox = Mailbox()
        stored, created = mailbox.submit(_command(status="authorized"))

        assert created is True
        assert stored.status == "received"

    def test_the_full_ladder_reaches_checkpointed(self):
        mailbox = Mailbox()
        mailbox.submit(_command())

        authorized = mailbox.authorize("cmd-1", SCOPES)
        assert authorized.status == "authorized"

        applied = mailbox.apply("cmd-1", current_plan_revision=1, current_execution_epoch=2)
        assert applied.status == "applied"

        checkpointed = mailbox.checkpoint("cmd-1")
        assert checkpointed.status == "checkpointed"
        assert mailbox.commands["cmd-1"].status == "checkpointed"

    def test_checkpoint_refuses_to_skip_forward(self):
        mailbox = Mailbox()
        mailbox.submit(_command())

        with pytest.raises(ValueError, match="refuses to skip"):
            mailbox.checkpoint("cmd-1")

    def test_apply_requires_an_authorized_command(self):
        mailbox = Mailbox()
        mailbox.submit(_command())

        with pytest.raises(ValueError, match="refuses to skip"):
            mailbox.apply("cmd-1", current_plan_revision=1, current_execution_epoch=2)

    def test_authorize_refuses_an_already_authorized_command(self):
        mailbox = Mailbox()
        mailbox.submit(_command())
        mailbox.authorize("cmd-1", SCOPES)

        with pytest.raises(ValueError, match="refuses to skip"):
            mailbox.authorize("cmd-1", SCOPES)

    def test_pending_lists_received_and_authorized_in_sequence_order(self):
        mailbox = Mailbox()
        mailbox.submit(_command(sequence=1, command_id="cmd-a", idempotency_key="k1"))
        mailbox.submit(
            _command(
                sequence=2,
                command_id="cmd-b",
                idempotency_key="k2",
                kind="steer",
                payload={"text": "fix the failing assertion first"},
            )
        )
        mailbox.submit(_command(sequence=3, command_id="cmd-c", idempotency_key="k3"))
        mailbox.submit(_command(sequence=4, command_id="cmd-d", idempotency_key="k4"))

        mailbox.authorize("cmd-a", SCOPES)
        mailbox.authorize("cmd-b", SCOPES)
        mailbox.apply("cmd-b", current_plan_revision=1, current_execution_epoch=2)

        pending = mailbox.pending("wp-demo-1")
        assert [command.command_id for command in pending] == ["cmd-a", "cmd-c", "cmd-d"]


class TestMailboxSequence:
    def test_out_of_order_sequence_is_refused(self):
        mailbox = Mailbox()
        mailbox.submit(_command(sequence=2, command_id="cmd-2", idempotency_key="k2"))

        with pytest.raises(ValueError, match="not strictly increasing"):
            mailbox.submit(_command(sequence=1, command_id="cmd-1", idempotency_key="k1"))

    def test_duplicate_sequence_is_refused(self):
        mailbox = Mailbox()
        mailbox.submit(_command(sequence=2, command_id="cmd-a", idempotency_key="k-first"))

        with pytest.raises(ValueError, match="not strictly increasing"):
            mailbox.submit(_command(sequence=2, command_id="cmd-b", idempotency_key="k-second"))

    def test_sequences_are_independent_per_work(self):
        mailbox = Mailbox()
        mailbox.submit(
            _command(work_id="wp-a", command_id="cmd-a", sequence=5, idempotency_key="ka")
        )

        stored, created = mailbox.submit(
            _command(work_id="wp-b", command_id="cmd-b", sequence=1, idempotency_key="kb")
        )

        assert created is True
        assert stored.status == "received"


class TestMailboxDedup:
    def test_redelivery_returns_the_existing_record_without_re_spending(self):
        mailbox = Mailbox()
        mailbox.submit(_command())
        mailbox.authorize("cmd-1", SCOPES)
        mailbox.apply("cmd-1", current_plan_revision=1, current_execution_epoch=2)
        mailbox.checkpoint("cmd-1")

        redelivered, created = mailbox.submit(_command())

        assert created is False
        assert redelivered.status == "checkpointed"
        assert redelivered.command_id == "cmd-1"
        assert mailbox.by_key["gitlab-note:1"] == "cmd-1"

    def test_redelivery_under_a_new_command_id_creates_no_second_command(self):
        mailbox = Mailbox()
        original, _ = mailbox.submit(_command())

        redelivered, created = mailbox.submit(
            _command(command_id="cmd-replayed", idempotency_key="gitlab-note:1")
        )

        assert created is False
        assert redelivered is original
        assert "cmd-replayed" not in mailbox.commands


class TestMailboxAuthorization:
    def test_actor_listed_under_its_own_origin_authorizes(self):
        mailbox = Mailbox()
        mailbox.submit(_command())

        authorized = mailbox.authorize("cmd-1", SCOPES)

        assert authorized.status == "authorized"

    def test_wrong_origin_actor_is_a_permission_error(self):
        mailbox = Mailbox()
        mailbox.submit(_command(actor_ref="human:reviewer-17", actor_origin="operator_token"))

        with pytest.raises(PermissionError, match="not listed under its own origin"):
            mailbox.authorize("cmd-1", SCOPES)

    def test_origin_missing_from_scopes_is_a_permission_error(self):
        mailbox = Mailbox()
        mailbox.submit(_command(actor_origin="automation_reconciler"))

        with pytest.raises(PermissionError, match="not listed under its own origin"):
            mailbox.authorize("cmd-1", {"server_authenticated_human": ("human:reviewer-17",)})


class TestMailboxCompareAndSet:
    def test_wrong_expected_plan_revision_expires_instead_of_applying(self):
        mailbox = Mailbox()
        mailbox.submit(_command(expected_plan_revision=1))
        mailbox.authorize("cmd-1", SCOPES)

        applied = mailbox.apply("cmd-1", current_plan_revision=2, current_execution_epoch=2)

        assert applied.status == "expired"

    def test_wrong_expected_execution_epoch_expires_instead_of_applying(self):
        mailbox = Mailbox()
        mailbox.submit(_command(expected_execution_epoch=2))
        mailbox.authorize("cmd-1", SCOPES)

        applied = mailbox.apply("cmd-1", current_plan_revision=1, current_execution_epoch=5)

        assert applied.status == "expired"

    def test_matching_expectations_apply(self):
        mailbox = Mailbox()
        mailbox.submit(_command(expected_plan_revision=1, expected_execution_epoch=2))
        mailbox.authorize("cmd-1", SCOPES)

        applied = mailbox.apply("cmd-1", current_plan_revision=1, current_execution_epoch=2)

        assert applied.status == "applied"

    def test_absent_expectations_never_expire(self):
        mailbox = Mailbox()
        mailbox.submit(_command())
        mailbox.authorize("cmd-1", SCOPES)

        applied = mailbox.apply("cmd-1", current_plan_revision=99, current_execution_epoch=99)

        assert applied.status == "applied"


class TestMailboxSurface:
    """The seam contract: the control plane codes against the surface, so
    the in-memory reference mailbox and the durable Postgres mailbox
    (forge.adaptive.mailbox_db — the same names, awaited) are two
    implementations of ONE protocol, not two APIs."""

    def test_the_in_memory_mailbox_satisfies_the_surface(self):
        assert isinstance(Mailbox(), MailboxSurface)

    def test_the_surface_is_exactly_the_protocol_callers_use(self):
        methods = {name for name in dir(MailboxSurface) if not name.startswith("_")}

        assert methods == {"submit", "authorize", "apply", "checkpoint", "pending"}


class TestRecordedPause:
    """NXT-09's dedup-first pause ordering: the command row is durable
    FIRST; only a NEW row (created=True) mutates pause_requested and bumps
    the publication epoch. A redelivered pause leaves the state UNCHANGED
    — the old order (fence the epoch, then discover the duplicate at
    submit) spent a generation the redelivery never earned."""

    def _pause_command(self, **overrides) -> ControlCommand:
        return _command(idempotency_key="note:pause:1", **overrides)

    def test_a_new_command_records_the_pause_and_bumps_the_fence(self):
        mailbox = Mailbox()
        state = PauseState(work_id="wp-demo-1")

        fenced, stored, created = recorded_pause(state, self._pause_command(), mailbox.submit)

        assert created is True
        assert stored.status == "received"  # the row exists BEFORE the fence
        assert fenced.pause_requested is True
        assert fenced.publication_epoch == 1

    def test_a_redelivered_pause_leaves_the_state_unchanged(self):
        mailbox = Mailbox()
        state = PauseState(work_id="wp-demo-1")
        fenced, _, _ = recorded_pause(state, self._pause_command(), mailbox.submit)

        again, stored, created = recorded_pause(
            fenced,
            self._pause_command(command_id="cmd-replayed", sequence=2),
            mailbox.submit,
        )

        assert created is False
        assert again is fenced  # identity: no epoch bump, nothing re-recorded
        assert again.publication_epoch == 1
        assert stored.command_id == "cmd-1"  # the winner's record, verbatim

    def test_a_failed_submit_records_nothing(self):
        """The submit runs BEFORE any state mutation — storage down means no
        fence, no pause bookkeeping, nothing to roll back."""

        class StorageDown:
            def submit(self, command: ControlCommand) -> tuple[ControlCommand, bool]:
                raise RuntimeError("database unavailable")

        with pytest.raises(RuntimeError, match="database unavailable"):
            recorded_pause(
                PauseState(work_id="wp-demo-1"), self._pause_command(), StorageDown().submit
            )


class TestOperatorControlServicePauseOrdering:
    """The shipped pause path: dedup BEFORE the epoch mutation (the same
    order recorded_pause imposes, whatever mailbox sits behind the seam)."""

    def test_a_duplicate_pause_does_not_bump_the_epoch(self):
        svc = OperatorControlService()

        first = svc.pause("wp-1", "human:op", "note:1")
        second = svc.pause("wp-1", "human:op", "note:1")

        assert first.publication_epoch == 1
        # The redelivery was refused by the mailbox BEFORE the fence —
        # the epoch is not bumped a second time (NXT-09 acceptance).
        assert second.publication_epoch == 1
        assert svc.pause_states["wp-1"].publication_epoch == 1
        pauses = [c for c in svc.mailbox.commands.values() if c.kind == "pause"]
        assert len(pauses) == 1

    def test_a_distinct_pause_bumps_the_epoch_once(self):
        svc = OperatorControlService()

        svc.pause("wp-1", "human:op", "note:1")
        second = svc.pause("wp-1", "human:op", "note:2")

        assert second.publication_epoch == 2
        pauses = [c for c in svc.mailbox.commands.values() if c.kind == "pause"]
        assert len(pauses) == 2


class TestPauseOrdering:
    def test_interrupt_without_a_pause_request_is_refused(self):
        with pytest.raises(ValueError, match="pause_requested is not on record"):
            send_interrupt(_paused_work_state())

    def test_request_pause_persists_the_flag_before_any_interrupt(self):
        state = _paused_work_state()

        paused = request_pause(state)

        # The ordering IS the guarantee: after request_pause the record
        # shows a pause even though no interrupt has gone out yet.
        assert paused.pause_requested is True
        assert paused.interrupt_sent is False

    def test_request_then_interrupt_sends_it(self):
        state = request_pause(_paused_work_state())

        interrupted = send_interrupt(state)

        assert interrupted.pause_requested is True
        assert interrupted.interrupt_sent is True


class TestPauseDrain:
    def test_cooperative_drain_captures_the_wip(self):
        state = send_interrupt(request_pause(_paused_work_state()))

        drained = drain_turn(state, cooperative=True)

        assert drained.checkpoint_captured is True
        assert drained.wip_artifact_id == "artifact:wip:wp-demo-1"

    def test_cooperative_drain_records_the_given_artifact(self):
        drained = drain_turn(
            _paused_work_state(), cooperative=True, wip_artifact_id="artifact:wip-42"
        )

        assert drained.wip_artifact_id == "artifact:wip-42"

    def test_timeout_drain_leaves_checkpoint_as_is_and_exposes_last_recoverable(self):
        state = send_interrupt(request_pause(_paused_work_state(last_applied_command_sequence=7)))

        drained = drain_turn(state, cooperative=False)

        # Never a false clean pause: nothing is captured by a timeout...
        assert drained.checkpoint_captured is False
        assert drained.wip_artifact_id is None
        # ...instead the last RECOVERABLE checkpoint sequence is exposed.
        assert drained.last_recoverable == 7

    def test_last_recoverable_is_the_sequence_the_checkpoint_carries(self):
        state = drain_turn(send_interrupt(request_pause(_paused_work_state())), cooperative=True)

        assert state.checkpoint_captured is True
        assert state.last_recoverable == state.last_applied_command_sequence


class TestPublicationEpoch:
    def test_new_publication_epoch_closes_old_epoch_authorizations(self):
        state = _paused_work_state(publication_epoch=3)

        bumped = new_publication_epoch(state)

        assert bumped.publication_epoch == 4
        assert bumped.publication_epoch > state.publication_epoch


class TestResumeCheck:
    @pytest.mark.parametrize(
        ("captured", "snapshot", "permissions"),
        [
            (False, True, True),
            (True, False, True),
            (True, True, False),
        ],
    )
    def test_resume_is_gated_on_all_three_conditions(self, captured, snapshot, permissions):
        state = _paused_work_state(checkpoint_captured=captured)

        ok, reason = resume_check(
            state,
            snapshot_available=snapshot,
            active_plan_revision=1,
            permissions_valid=permissions,
        )

        assert ok is False
        assert reason != ""

    def test_confirmed_checkpoint_with_snapshot_and_permissions_resumes(self):
        state = _paused_work_state(checkpoint_captured=True)

        ok, reason = resume_check(
            state, snapshot_available=True, active_plan_revision=4, permissions_valid=True
        )

        assert ok is True
        assert "resumable" in reason

    def test_the_plan_revision_is_recorded_not_compared(self):
        state = _paused_work_state(checkpoint_captured=True)

        # Whatever revision is active now, historical spend stays attached
        # to the same work — the revision is information, not a gate.
        ok, reason = resume_check(
            state, snapshot_available=True, active_plan_revision=11, permissions_valid=True
        )

        assert ok is True
        assert "11" in reason


class TestNewExecutionEpoch:
    def test_reconstructs_from_durable_artifacts_by_default(self):
        epoch = new_execution_epoch("attempt-demo-2", prior_epoch=2)

        assert epoch == {
            "attempt_id": "attempt-demo-2",
            "execution_epoch": 3,
            "native_session_restored": False,
            "reconstruction": "durable_artifacts",
        }

    @pytest.mark.parametrize("profile", ["claude-sdk", "codex-app", "opencode-server"])
    def test_native_session_restored_only_for_a_pinned_profile(self, profile):
        epoch = new_execution_epoch("attempt-2", prior_epoch=4, pinned_profile=profile)

        assert epoch["native_session_restored"] is True
        assert epoch["execution_epoch"] == 5

    def test_an_unknown_profile_reconstructs(self):
        epoch = new_execution_epoch("attempt-2", prior_epoch=1, pinned_profile="beta-driver")

        assert epoch["native_session_restored"] is False
        assert epoch["reconstruction"] == "durable_artifacts"


class TestClassifyInstruction:
    @pytest.mark.parametrize(
        "text",
        [
            "fix the failing assertion first",
            "use the existing helper",
            "start with the parser before the CLI",
        ],
    )
    def test_guidance_within_the_current_work_is_steer(self, text):
        assert classify_instruction(text) == "steer"

    @pytest.mark.parametrize(
        "text",
        [
            "do not introduce a new broker",
            "must use RabbitMQ",
            "use existing database helpers instead of a new queue",
        ],
    )
    def test_contract_constraints_are_amend(self, text):
        assert classify_instruction(text) == "amend"

    @pytest.mark.parametrize(
        "text",
        [
            "skip the tests",
            "turn off checks",
            "make tests optional",
        ],
    )
    def test_weakening_the_tests_is_acceptance_change_not_steering(self, text):
        assert classify_instruction(text) == "acceptance_change"

    def test_acceptance_weakening_wins_even_with_constraint_wording(self):
        assert classify_instruction("skip the tests for the new database") == ("acceptance_change")


class TestDeliverSteer:
    def test_bounded_guidance_is_accepted_at_a_turn_boundary(self):
        command = _command(
            kind="steer", idempotency_key="steer:1", payload={"text": "fix the parser first"}
        )

        delivered = deliver_steer(command, current_turn=12)

        assert delivered == {
            "command_id": "cmd-1",
            "delivered_at_turn": 12,
            "status": "accepted",
        }

    def test_acceptance_policy_changes_are_rejected(self):
        command = _command(
            kind="steer", idempotency_key="steer:2", payload={"text": "skip the tests"}
        )

        delivered = deliver_steer(command, current_turn=12)

        assert delivered["status"] == "rejected"
        assert delivered["reason"] == "acceptance policy change requires the revision gate"


class TestPromoteToProposal:
    def test_amend_text_promotes_to_a_material_contract_proposal(self):
        text = "do not introduce a new broker"

        proposal = promote_to_proposal(text)

        expected_id = f"auto-{hashlib.sha1(text.encode()).hexdigest()[:12]}"
        assert proposal is not None
        assert proposal.proposal_id == expected_id
        assert proposal.classification == "material_contract"
        assert proposal.rationale == text
        assert proposal.work_id == "unknown"
        assert proposal.from_revision == 1

    def test_the_proposal_id_is_a_deterministic_digest_of_the_text(self):
        first = promote_to_proposal("must use RabbitMQ")
        second = promote_to_proposal("must use RabbitMQ")

        assert first is not None
        assert first.proposal_id == second.proposal_id

    @pytest.mark.parametrize(
        "text",
        [
            "fix the failing assertion first",
            "use the existing helper",
            "skip the tests",
        ],
    )
    def test_non_amend_text_promotes_to_nothing(self, text):
        assert promote_to_proposal(text) is None


class TestCancelContract:
    @pytest.mark.parametrize(
        "stage",
        [
            "discovery",
            "implementation",
            "questions",
            "revisions",
            "verification",
            "child_work_items",
        ],
    )
    def test_cancellation_generation_applies_to_every_interactive_stage(self, stage):
        assert cancel_generation_applies(stage) == [
            "discovery",
            "implementation",
            "questions",
            "revisions",
            "verification",
            "child_work_items",
        ]

    def test_an_unknown_stage_is_a_typo_not_a_silent_skip(self):
        with pytest.raises(ValueError, match="unknown interactive stage"):
            cancel_generation_applies("questons")

    def test_final_cancel_revokes_grants_and_retains_artifacts(self):
        state = _paused_work_state(publication_epoch=2)

        cancelled = final_cancel(state, accepted_effects=["effect:pub-1"])

        assert cancelled["revoked"] == ["tool_grants", "publication_grants"]
        assert cancelled["retained_artifacts"] is True
        assert cancelled["restart_requires_new_command"] is True
        assert cancelled["cancellation_generation"] == 2

    def test_final_cancel_correlates_accepted_effects_never_claims_them_undone(self):
        cancelled = final_cancel(
            _paused_work_state(), accepted_effects=["effect:pub-1", "effect:pub-2"]
        )

        # Correlated as evidence — the record retains them, it never
        # asserts they were rolled back.
        assert cancelled["correlated_effects"] == ["effect:pub-1", "effect:pub-2"]

    def test_the_cancel_generation_is_the_publication_epoch_fence(self):
        state = new_publication_epoch(_paused_work_state(publication_epoch=1))

        cancelled = final_cancel(state, accepted_effects=[])

        assert cancelled["cancellation_generation"] == 2

    def test_a_late_effect_accepted_before_cancel_is_superseded(self):
        assert late_effect_outcome(True) == "superseded"

    def test_a_late_effect_after_the_boundary_is_forbidden(self):
        assert late_effect_outcome(False) == "forbidden"
