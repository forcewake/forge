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
from dataclasses import dataclass, replace

import pytest

from forge.adaptive.control import (
    BroadcastCommand,
    BroadcastMailbox,
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
    recorded_pause_async,
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


@dataclass(frozen=True)
class _Receipt:
    """A minimal real-looking capture receipt (the CaptureResult contract)."""

    artifact_id: str = "a" * 64
    digest: str = "a" * 64
    sequence: int = 0
    verified: bool = True


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


class TestRecordedPauseAsync:
    """The awaitable twin: the SAME NXT-09 gate over the async surface
    (the service awaits its mailbox whatever backs it)."""

    def _pause_command(self, **overrides) -> ControlCommand:
        return _command(idempotency_key="note:pause:1", **overrides)

    @staticmethod
    def _async_submit_over(mailbox: Mailbox):
        async def submit(command: ControlCommand) -> tuple[ControlCommand, bool]:
            return mailbox.submit(command)

        return submit

    async def test_a_new_command_records_the_pause_and_bumps_the_fence(self):
        mailbox = Mailbox()
        state = PauseState(work_id="wp-demo-1")

        fenced, stored, created = await recorded_pause_async(
            state, self._pause_command(), self._async_submit_over(mailbox)
        )

        assert created is True
        assert stored.status == "received"  # the row exists BEFORE the fence
        assert fenced.pause_requested is True
        assert fenced.publication_epoch == 1

    async def test_a_redelivered_pause_leaves_the_state_unchanged(self):
        mailbox = Mailbox()
        state = PauseState(work_id="wp-demo-1")
        fenced, _, _ = await recorded_pause_async(
            state, self._pause_command(), self._async_submit_over(mailbox)
        )

        again, stored, created = await recorded_pause_async(
            fenced,
            self._pause_command(command_id="cmd-replayed", sequence=2),
            self._async_submit_over(mailbox),
        )

        assert created is False
        assert again is fenced  # identity: no epoch bump, nothing re-recorded
        assert again.publication_epoch == 1
        assert stored.command_id == "cmd-1"  # the winner's record, verbatim


class TestOperatorControlServicePauseOrdering:
    """The shipped pause path: dedup BEFORE the epoch mutation (the same
    order recorded_pause imposes, whatever mailbox sits behind the seam)."""

    async def test_a_duplicate_pause_does_not_bump_the_epoch(self):
        svc = OperatorControlService()

        first = await svc.pause("wp-1", "human:op", "note:1")
        second = await svc.pause("wp-1", "human:op", "note:1")

        assert first.publication_epoch == 1
        # The redelivery was refused by the mailbox BEFORE the fence —
        # the epoch is not bumped a second time (NXT-09 acceptance).
        assert second.publication_epoch == 1
        assert svc.pause_states["wp-1"].publication_epoch == 1
        pauses = [c for c in svc.mailbox.commands.values() if c.kind == "pause"]
        assert len(pauses) == 1

    async def test_a_distinct_pause_bumps_the_epoch_once(self):
        svc = OperatorControlService()

        await svc.pause("wp-1", "human:op", "note:1")
        second = await svc.pause("wp-1", "human:op", "note:2")

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
    """NXT-15: the cooperative drain requires a REAL capture. The old
    behavior — fabricating ``artifact:wip:<work_id>`` and calling the
    pause captured — is the regression these tests pin out of existence."""

    def test_cooperative_drain_without_capture_lands_partial_never_fabricated(self):
        state = send_interrupt(request_pause(_paused_work_state()))

        drained = drain_turn(state, cooperative=True)

        # The fabricated artifact id is GONE (old behavior failed):
        assert drained.wip_artifact_id is None
        assert drained.checkpoint_receipt is None
        # The honest outcome: a cooperative boundary, nothing durable captured.
        assert drained.pause_status == "paused_partial"
        # checkpoint_captured remains the bridge-level cooperative-boundary
        # claim (the steering bridge's contract); the DURABLE truth is the
        # pause_status — which says partial.
        assert drained.checkpoint_captured is True
        # last_recoverable falls back to the applied watermark: the number
        # the mailbox actually applied, never an invented checkpoint.
        assert drained.last_recoverable == drained.last_applied_command_sequence

    def test_cooperative_drain_with_a_real_capture_lands_paused_with_a_receipt(self):
        state = send_interrupt(request_pause(_paused_work_state(last_applied_command_sequence=9)))

        receipt = _Receipt(sequence=9)
        drained = drain_turn(state, cooperative=True, capture=lambda: receipt)

        assert drained.pause_status == "paused"
        assert drained.checkpoint_captured is True
        assert drained.wip_artifact_id == receipt.artifact_id
        assert drained.checkpoint_receipt is receipt
        assert drained.last_checkpoint_sequence == 9
        assert drained.last_recoverable == 9
        assert drained.failure_reason == ""

    def test_a_failed_capture_lands_paused_failed_with_the_last_real_checkpoint(self):
        """Disk full during upload: the pause says FAILED, and the last REAL
        checkpoint (sequence 5, committed by an earlier capture) is what
        last_recoverable names — never a fabricated success."""

        def disk_full() -> _Receipt:
            raise RuntimeError("disk full during artifact upload")

        state = send_interrupt(
            request_pause(
                _paused_work_state(last_applied_command_sequence=7, last_checkpoint_sequence=5)
            )
        )

        drained = drain_turn(state, cooperative=True, capture=disk_full)

        assert drained.pause_status == "paused_failed"
        assert drained.checkpoint_captured is False
        assert drained.wip_artifact_id is None
        assert drained.checkpoint_receipt is None
        assert "disk full" in drained.failure_reason
        assert drained.last_recoverable == 5  # the last REAL checkpoint

    def test_an_unverified_receipt_is_never_confirmed(self):
        state = send_interrupt(request_pause(_paused_work_state()))

        drained = drain_turn(state, cooperative=True, capture=lambda: _Receipt(verified=False))

        assert drained.pause_status == "paused_failed"
        assert drained.checkpoint_captured is False
        assert drained.wip_artifact_id is None
        assert "unverified" in drained.failure_reason

    def test_cooperative_drain_records_a_caller_supplied_real_artifact(self):
        drained = drain_turn(
            _paused_work_state(), cooperative=True, wip_artifact_id="sha256:abc123"
        )

        assert drained.pause_status == "paused"
        assert drained.wip_artifact_id == "sha256:abc123"  # recorded verbatim
        assert drained.checkpoint_captured is True

    def test_timeout_drain_leaves_checkpoint_as_is_and_exposes_last_recoverable(self):
        state = send_interrupt(request_pause(_paused_work_state(last_applied_command_sequence=7)))

        drained = drain_turn(state, cooperative=False)

        # Never a false clean pause: nothing is captured by a timeout...
        assert drained.checkpoint_captured is False
        assert drained.wip_artifact_id is None
        assert drained.pause_status == "paused_failed"  # terminated, not saved
        # ...instead the last RECOVERABLE checkpoint sequence is exposed.
        assert drained.last_recoverable == 7

    def test_redraining_a_decided_pause_is_an_idempotent_noop(self):
        state = drain_turn(
            send_interrupt(request_pause(_paused_work_state())),
            cooperative=True,
            capture=lambda: _Receipt(sequence=3),
        )

        assert drain_turn(state, cooperative=False) is state
        assert drain_turn(state, cooperative=True).pause_status == "paused"

    def test_last_recoverable_is_the_sequence_the_checkpoint_carries(self):
        state = drain_turn(send_interrupt(request_pause(_paused_work_state())), cooperative=True)

        assert state.checkpoint_captured is True
        assert state.last_recoverable == state.last_applied_command_sequence

    def test_request_pause_opens_the_pausing_leg_of_the_status(self):
        state = _paused_work_state()

        assert request_pause(state).pause_status == "pausing"
        # A decided pause is never reopened by a redelivered request.
        decided = replace(request_pause(state), pause_status="paused")
        assert request_pause(decided).pause_status == "paused"


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

    def test_strict_mode_refuses_a_pause_without_a_verified_receipt(self):
        """NXT-18's strict gate: the cooperative-boundary claim (the bridge's
        legacy flag) is NOT a durable checkpoint — partial and failed
        pauses refuse, and the reason says which leg was missing."""

        def _receipted(status: str, *, receipt: object) -> PauseState:
            return _paused_work_state(
                checkpoint_captured=True, pause_status=status, checkpoint_receipt=receipt
            )  # type: ignore[arg-type]

        partial = _receipted("paused_partial", receipt=None)
        failed = _receipted("paused_failed", receipt=_Receipt())
        unverified = _receipted("paused", receipt=_Receipt(verified=False))

        for state in (partial, failed, unverified):
            ok, reason = resume_check(
                state,
                snapshot_available=True,
                active_plan_revision=1,
                permissions_valid=True,
                require_verified_checkpoint=True,
            )
            assert ok is False
            assert "no verified durable checkpoint" in reason

    def test_strict_mode_accepts_only_a_paused_state_with_a_verified_receipt(self):
        state = _paused_work_state(
            checkpoint_captured=True, pause_status="paused", checkpoint_receipt=_Receipt()
        )

        ok, reason = resume_check(
            state,
            snapshot_available=True,
            active_plan_revision=1,
            permissions_valid=True,
            require_verified_checkpoint=True,
        )

        assert ok is True
        assert "resumable" in reason


class TestNewExecutionEpoch:
    def test_reconstructs_from_durable_artifacts_by_default(self):
        epoch = new_execution_epoch("attempt-demo-2", prior_epoch=2)

        assert epoch == {
            "attempt_id": "attempt-demo-2",
            "execution_epoch": 3,
            "native_session_restored": False,
            "reconstruction": "durable_artifacts",
            "native_restore_evidence": None,
        }

    @pytest.mark.parametrize("profile", ["claude-sdk", "codex-app", "opencode-server"])
    def test_a_pinned_profile_name_alone_claims_no_native_restore(self, profile):
        """NXT-18: profile MEMBERSHIP is compatibility, not a completed
        restore — the old behavior (native_session_restored=True from the
        name alone) failed exactly here."""
        epoch = new_execution_epoch("attempt-2", prior_epoch=4, pinned_profile=profile)

        assert epoch["native_session_restored"] is False
        assert epoch["reconstruction"] == "durable_artifacts"

    @pytest.mark.parametrize("profile", ["claude-sdk", "codex-app", "opencode-server"])
    def test_a_pinned_profile_with_verified_restore_evidence_restores_natively(self, profile):
        epoch = new_execution_epoch(
            "attempt-2",
            prior_epoch=4,
            pinned_profile=profile,
            native_restore_evidence="b" * 64,
        )

        assert epoch["native_session_restored"] is True
        assert epoch["reconstruction"] == "native_session"
        assert epoch["native_restore_evidence"] == "b" * 64
        assert epoch["execution_epoch"] == 5

    def test_an_unknown_profile_never_restores_natively_even_with_evidence(self):
        epoch = new_execution_epoch(
            "attempt-2",
            prior_epoch=1,
            pinned_profile="beta-driver",
            native_restore_evidence="b" * 64,
        )

        assert epoch["native_session_restored"] is False
        assert epoch["reconstruction"] == "durable_artifacts"

    def test_evidence_without_a_pinned_profile_reconstructs(self):
        epoch = new_execution_epoch("attempt-2", prior_epoch=1, native_restore_evidence="b" * 64)

        assert epoch["native_session_restored"] is False
        assert epoch["reconstruction"] == "durable_artifacts"

    def test_the_epoch_is_always_fresh(self):
        assert new_execution_epoch("a", prior_epoch=41)["execution_epoch"] == 42


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


class TestBroadcastCommand:
    """NXT-13 — the work-wide barrier: one parent intent, one FIXED
    recipient set, one acknowledgement row per lane. The parent is
    completed by EVERY recipient, never by the first one to speak."""

    def test_two_lanes_each_ack_and_the_parent_reflects_both(self):
        mailbox = BroadcastMailbox()
        broadcast, created = mailbox.submit(_command(kind="pause"), ("lane-1", "lane-2"))

        assert created is True
        assert broadcast.status == "pending"
        assert broadcast.recipients == ("lane-1", "lane-2")

        first = mailbox.acknowledge(broadcast.command_id, "lane-1", note="ckpt:aaa")
        assert first.status == "pending"  # lane-2 has not spoken — no barrier yet

        second = mailbox.acknowledge(broadcast.command_id, "lane-2", note="ckpt:bbb")
        assert second.status == "completed"
        assert second.acknowledged_recipients == ("lane-1", "lane-2")

    def test_one_child_outcome_cannot_be_hidden_by_another(self):
        """One lane's success never completes the barrier while the other is
        undecided — and an uncertain lane stays NAMED, not averaged away."""
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(kind="pause"), ("lane-1", "lane-2"))

        mailbox.acknowledge(broadcast.command_id, "lane-1", note="ckpt:aaa")
        uncertain = mailbox.mark_uncertain(
            broadcast.command_id, "lane-2", "probe inconclusive: session gone"
        )

        assert uncertain.status == "completed_with_uncertain"
        assert uncertain.uncertain_recipients == ("lane-2",)
        assert uncertain.acknowledged_recipients == ("lane-1",)

    def test_an_uncertain_lane_resolves_only_with_evidence(self):
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(kind="pause"), ("lane-1", "lane-2"))

        mailbox.mark_uncertain(broadcast.command_id, "lane-1", "probe inconclusive")
        with pytest.raises(ValueError, match="never re-acknowledged blind"):
            mailbox.acknowledge(broadcast.command_id, "lane-1")

        resolved = mailbox.resolve_uncertain(
            broadcast.command_id, "lane-1", note="late checkpoint receipt verified"
        )
        mailbox.acknowledge(broadcast.command_id, "lane-2")
        assert resolved.acknowledgement("lane-1").status == "acknowledged"
        assert mailbox.broadcast(broadcast.command_id).status == "completed"

    def test_an_empty_or_duplicated_recipient_set_is_refused(self):
        mailbox = BroadcastMailbox()
        with pytest.raises(ValueError, match="empty recipients"):
            mailbox.submit(_command(kind="pause"), ())
        with pytest.raises(ValueError, match="recipients must be unique"):
            mailbox.submit(_command(kind="pause"), ("lane-1", "lane-1"))

    def test_the_view_scopes_the_command_to_the_recipient_run(self):
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(
            _command(kind="pause", payload={"reason": "operator stop"}), ("lane-1", "lane-2")
        )

        view = broadcast.view_for("lane-1")

        assert view is not None
        assert view.command_id == broadcast.command_id  # the ack key is the parent
        assert view.payload["run_id"] == "lane-1"  # a foreign lane's scope check refuses it
        assert view.payload["broadcast_id"] == broadcast.command_id
        assert view.payload["reason"] == "operator stop"
        assert broadcast.view_for("lane-stranger") is None

    def test_a_stranger_lane_can_never_acknowledge(self):
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(kind="pause"), ("lane-1",))

        with pytest.raises(KeyError, match="not in the frozen recipient set"):
            mailbox.acknowledge(broadcast.command_id, "lane-stranger")


class TestBroadcastMailbox:
    """The NXT-13 defect itself: one command, one status, first lane wins.
    The per-recipient views are the fix — each lane consumes independently."""

    def test_the_second_lane_still_sees_the_pause_after_the_first_acked(self):
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(kind="pause"), ("lane-1", "lane-2"))
        assert len(mailbox.pending_for("wp-demo-1", "lane-1")) == 1
        assert len(mailbox.pending_for("wp-demo-1", "lane-2")) == 1

        mailbox.acknowledge(broadcast.command_id, "lane-1", note="checkpointed")

        # THE regression: lane-1's consumption removed nothing from lane-2.
        assert mailbox.pending_for("wp-demo-1", "lane-1") == []
        still_pending = mailbox.pending_for("wp-demo-1", "lane-2")
        assert [command.command_id for command in still_pending] == [broadcast.command_id]
        assert still_pending[0].payload["run_id"] == "lane-2"

    def test_a_work_scoped_command_never_enters_the_single_consumer_queue(self):
        mailbox = BroadcastMailbox()
        mailbox.submit(
            _command(sequence=1, command_id="cmd-wide", idempotency_key="k-wide", kind="pause"),
            ("lane-1", "lane-2"),
        )
        mailbox.mailbox.submit(
            _command(
                sequence=2,
                command_id="cmd-plain",
                idempotency_key="k-plain",
                kind="steer",
                payload={"text": "fix the parser first"},
            )
        )

        # The single-lane view carries the ordinary steer only — a lane
        # draining it can never consume (and thereby hide) the broadcast.
        assert [command.command_id for command in mailbox.mailbox.pending("wp-demo-1")] == [
            "cmd-plain"
        ]
        # ...and the broadcast is visible ONLY through per-recipient views.
        assert len(mailbox.pending_for("wp-demo-1", "lane-1")) == 1
        assert len(mailbox.pending_for("wp-demo-1", "lane-2")) == 1

    def test_a_run_scoped_command_cannot_become_a_broadcast(self):
        mailbox = BroadcastMailbox()
        with pytest.raises(ValueError, match="cannot become a work-wide broadcast"):
            mailbox.submit(
                _command(payload={"run_id": "lane-1"}),
                ("lane-1", "lane-2"),
            )

    def test_redelivery_adopts_the_winner_and_spends_nothing(self):
        mailbox = BroadcastMailbox()
        broadcast, created_first = mailbox.submit(_command(), ("lane-1", "lane-2"))
        mailbox.acknowledge(broadcast.command_id, "lane-1", note="ckpt:aaa")

        replayed, created_second = mailbox.submit(
            _command(command_id="cmd-replayed", sequence=2), ("lane-1", "lane-2")
        )

        assert created_first is True
        assert created_second is False
        assert replayed.command_id == broadcast.command_id
        assert replayed.acknowledgement("lane-1").status == "acknowledged"  # state kept
        assert replayed.acknowledgement("lane-2").status == "pending"

    def test_a_duplicate_work_wide_delivery_does_not_duplicate_child_interrupts(self):
        """The replay's recipient list is discarded with its other bytes: the
        winner's frozen set is authoritative, so one redelivered gateway event
        cannot mint a second fan-out."""
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(), ("lane-1", "lane-2"))

        replayed, created = mailbox.submit(
            _command(command_id="cmd-replayed", sequence=2), ("lane-1", "lane-2", "lane-3")
        )

        assert created is False
        assert replayed.recipients == ("lane-1", "lane-2")  # lane-3 never joined

    def test_the_fence_holds_while_a_pause_is_undecided(self):
        """A child created during the pause is not in the frozen set: it must
        not start through the fence, and it cannot self-acknowledge."""
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(kind="pause"), ("lane-1", "lane-2"))

        assert mailbox.fence_active("wp-demo-1") is True  # a new lane waits

        mailbox.acknowledge(broadcast.command_id, "lane-1")
        assert mailbox.fence_active("wp-demo-1") is True  # lane-2 still outstanding

        mailbox.acknowledge(broadcast.command_id, "lane-2")
        assert mailbox.fence_active("wp-demo-1") is False

    def test_an_uncertain_completion_lifts_the_fence_but_keeps_the_name(self):
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(kind="pause"), ("lane-1", "lane-2"))
        mailbox.acknowledge(broadcast.command_id, "lane-1")
        mailbox.mark_uncertain(broadcast.command_id, "lane-2", "probe inconclusive")

        finished = mailbox.broadcast(broadcast.command_id)

        assert finished.status == "completed_with_uncertain"
        assert mailbox.fence_active("wp-demo-1") is False  # decided, with the subset named
        assert finished.uncertain_recipients == ("lane-2",)

    def test_re_acknowledging_is_an_idempotent_noop_and_undeciding_refused(self):
        mailbox = BroadcastMailbox()
        broadcast, _ = mailbox.submit(_command(), ("lane-1",))
        mailbox.acknowledge(broadcast.command_id, "lane-1", note="ckpt:aaa")

        again = mailbox.acknowledge(broadcast.command_id, "lane-1", note="ckpt:aaa")

        assert again.acknowledgement("lane-1").note == "ckpt:aaa"  # nothing re-spent
        with pytest.raises(ValueError, match="only a pending recipient"):
            mailbox.mark_uncertain(broadcast.command_id, "lane-1", "late doubt")

    def test_an_unknown_broadcast_is_a_key_error(self):
        mailbox = BroadcastMailbox()
        with pytest.raises(KeyError, match="unknown broadcast command_id"):
            mailbox.acknowledge("cmd-nope", "lane-1")


class TestOperatorControlServiceWorkScopedPause:
    """The shipped shape: pause(work_scoped=True, recipients=the work's
    lanes) — additive; the single-lane path is untouched."""

    async def test_a_work_wide_pause_fans_out_and_completes_on_every_ack(self):
        svc = OperatorControlService()

        broadcast = await svc.pause(
            "wp-1", "human:op", "note:wide-1", work_scoped=True, recipients=("run-a", "run-b")
        )

        assert isinstance(broadcast, BroadcastCommand)
        assert broadcast.status == "pending"
        # CTL-05's ordering holds at the parent: pause on record, interrupt out.
        assert svc.pause_states["wp-1"].pause_requested is True
        assert svc.pause_states["wp-1"].interrupt_sent is True
        assert svc.pause_states["wp-1"].publication_epoch == 1
        # both lanes see their own view through the per-recipient drain
        for lane in ("run-a", "run-b"):
            view = await svc.pending_for("wp-1", lane)
            assert [command.command_id for command in view] == [broadcast.command_id]
            assert view[0].payload["run_id"] == lane

        await svc.acknowledge(broadcast.command_id, "run-a", note="ckpt:aaa")
        half = await svc.broadcast(broadcast.command_id)
        assert half.status == "pending"
        assert await svc.pending_for("wp-1", "run-a") == []  # its own view is consumed
        assert len(await svc.pending_for("wp-1", "run-b")) == 1  # the second lane still sees it

        await svc.acknowledge(broadcast.command_id, "run-b", note="ckpt:bbb")
        assert (await svc.broadcast(broadcast.command_id)).status == "completed"
        assert await svc.fence_active("wp-1") is False

    async def test_a_duplicate_work_wide_pause_does_not_bump_the_epoch_or_refan(self):
        svc = OperatorControlService()

        first = await svc.pause(
            "wp-1", "human:op", "note:wide-1", work_scoped=True, recipients=("run-a", "run-b")
        )
        await svc.acknowledge(first.command_id, "run-a", note="ckpt:aaa")
        second = await svc.pause(
            "wp-1", "human:op", "note:wide-1", work_scoped=True, recipients=("run-a", "run-b")
        )

        assert second.command_id == first.command_id  # the winner, verbatim
        assert svc.pause_states["wp-1"].publication_epoch == 1  # NOT bumped twice
        assert second.acknowledgement("run-a").status == "acknowledged"  # state kept
        assert [b.command_id for b in await svc.broadcasts("wp-1")] == [first.command_id]
        assert await svc.fence_active("wp-1") is True  # run-b is still outstanding

    async def test_an_uncertain_lane_is_individually_visible_at_the_parent(self):
        svc = OperatorControlService()
        broadcast = await svc.pause(
            "wp-1", "human:op", "note:wide-1", work_scoped=True, recipients=("run-a", "run-b")
        )

        await svc.acknowledge(broadcast.command_id, "run-a", note="ckpt:aaa")
        uncertain = await svc.mark_uncertain(
            broadcast.command_id, "run-b", "runner died before acknowledgement"
        )

        assert uncertain.status == "completed_with_uncertain"
        assert uncertain.uncertain_recipients == ("run-b",)
        assert await svc.pending_for("wp-1", "run-b") == []  # it will not re-deliver blindly

    async def test_work_scoped_requires_a_recipient_set(self):
        svc = OperatorControlService()
        with pytest.raises(ValueError, match="empty recipients"):
            await svc.pause("wp-1", "human:op", "note:wide-1", work_scoped=True)

    async def test_the_single_lane_pause_path_is_unchanged(self):
        """Back-compat: the default pause is still the CTL-05 single-lane
        command — a PauseState in, a PauseState out, one mailbox row."""
        svc = OperatorControlService()

        state = await svc.pause("wp-1", "human:op", "note:1")

        assert isinstance(state, PauseState)
        assert state.pause_requested is True
        assert state.interrupt_sent is True
        assert await svc.fence_active("wp-1") is False  # no broadcast was created
        assert await svc.broadcasts("wp-1") == []
        assert len(svc.mailbox.pending("wp-1")) == 1  # the ordinary pause row

    async def test_pending_for_merges_the_lane_view_in_sequence_order(self):
        svc = OperatorControlService()
        await svc.steer("wp-1", "human:op", "fix the parser first")  # ordinary pending
        broadcast = await svc.pause(
            "wp-1", "human:op", "note:wide-1", work_scoped=True, recipients=("run-a",)
        )

        combined = await svc.pending_for("wp-1", "run-a")

        assert [command.command_id for command in combined] == [
            svc.mailbox.pending("wp-1")[0].command_id,
            broadcast.command_id,
        ]
        # a foreign lane sees the steer but never run-a's broadcast view
        assert [command.kind for command in await svc.pending_for("wp-1", "run-b")] == ["steer"]


class TestAsyncSurfaceOverMemory:
    """The unified async surface over the IN-MEMORY implementation: the
    service's own surface round-trips submit -> pending -> the ladder ->
    checkpoint without touching the raw sync mailbox (the durable twin
    of the same flow lives in tests/test_adaptive_mailbox_db.py)."""

    async def test_the_memory_surface_round_trips_submit_pending_checkpoint(self):
        svc = OperatorControlService()
        command = _command(kind="steer", payload={"text": "fix the parser first"})

        stored, created = await svc.submit(command)
        assert created is True
        assert stored.status == "received"
        assert [c.command_id for c in await svc.pending("wp-demo-1")] == ["cmd-1"]

        authorized = await svc.surface.authorize("cmd-1", SCOPES)
        assert authorized.status == "authorized"
        applied = await svc.surface.apply(
            "cmd-1", current_plan_revision=1, current_execution_epoch=1
        )
        assert applied.status == "applied"
        checkpointed = await svc.surface.checkpoint("cmd-1")
        assert checkpointed.status == "checkpointed"
        assert await svc.pending("wp-demo-1") == []  # a spent command never re-delivers

    async def test_a_redelivery_through_the_surface_spends_nothing(self):
        svc = OperatorControlService()
        await svc.submit(_command())

        replayed, created = await svc.submit(
            _command(command_id="cmd-replayed", sequence=2, status="checkpointed")
        )

        assert created is False
        assert replayed.command_id == "cmd-1"
        assert replayed.status == "received"  # the replay's bytes are discarded

    async def test_next_sequence_is_per_work_not_per_process(self):
        svc = OperatorControlService()

        assert await svc.surface.next_sequence("wp-demo-1") == 1
        await svc.submit(_command(work_id="wp-demo-1"))
        assert await svc.surface.next_sequence("wp-demo-1") == 2
        # a DIFFERENT work allocates from its own history:
        assert await svc.surface.next_sequence("wp-other") == 1


class TestControlServiceFromEnv:
    """The flag-gated mount: FORGE_CONTROL_MAILBOX=postgres selects the
    durable mailbox when a session factory is available; everything else
    (default, typo, no factory) is the in-memory reference."""

    def test_the_default_is_the_in_memory_mailbox(self):
        from forge.adaptive.mailbox_bridge import AsyncMailboxAdapter
        from forge.adaptive.mailbox_db import PostgresMailbox
        from forge.adaptive.wiring import FORGE_CONTROL_MAILBOX_ENV, control_service_from_env

        assert FORGE_CONTROL_MAILBOX_ENV == "FORGE_CONTROL_MAILBOX"
        svc = control_service_from_env(env={})
        assert isinstance(svc.mailbox, Mailbox)
        assert isinstance(svc.surface, AsyncMailboxAdapter)
        assert not isinstance(svc.mailbox, PostgresMailbox)

    async def test_postgres_mounts_over_a_supplied_session_factory(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from forge.adaptive.mailbox_db import PostgresMailbox
        from forge.adaptive.wiring import control_service_from_env

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        try:
            svc = control_service_from_env(
                env={"FORGE_CONTROL_MAILBOX": "postgres"},
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
            )
            assert isinstance(svc.mailbox, PostgresMailbox)
            assert svc.surface is svc.mailbox  # the durable mailbox IS the surface
        finally:
            await engine.dispose()

    def test_postgres_without_any_factory_falls_back_to_memory(self, caplog):
        from forge.adaptive.wiring import control_service_from_env

        svc = control_service_from_env(env={"FORGE_CONTROL_MAILBOX": "postgres"})
        assert isinstance(svc.mailbox, Mailbox)  # honest fallback, loudly said
        assert any("did not take effect" in record.message for record in caplog.records)

    def test_a_misspelled_mode_is_memory_not_postgres(self):
        from forge.adaptive.wiring import control_service_from_env

        svc = control_service_from_env(env={"FORGE_CONTROL_MAILBOX": "postgre"})
        assert isinstance(svc.mailbox, Mailbox)
