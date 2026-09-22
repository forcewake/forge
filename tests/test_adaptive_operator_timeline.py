"""The operator timeline (R28-29) — request, effect and evidence, distinguished.

``timeline_from_journal`` is a PURE projection over heterogeneous journal
rows: the lane's append-only SteeringAction journal, the channel's
error/evidence rows, control-plane command rows and the durable mailbox's
audit entries. Each row proves exactly ONE category, the projection
preserves journal order, and a row that carries nothing classifiable is
skipped — never guessed into a category. The router's ``control_timeline``
/ ``timeline_note_section`` expose the same rows as the ``/status``
``timeline`` section.
"""

from __future__ import annotations

from dataclasses import asdict

from forge.adaptive.adapters import ClaudeSDKAdapter
from forge.adaptive.command_router import control_timeline, timeline_note_section
from forge.adaptive.lane_control import LaneSteeringSession, SteeringAction
from forge.adaptive.models import ControlCommand
from forge.adaptive.operator_timeline import (
    TIMELINE_CATEGORIES,
    timeline_from_journal,
    timeline_rows,
)
from forge.adaptive.wiring import OperatorControlService


class FakeClaudeClient:
    async def start_session(self, task: str) -> str:
        return "sess-1"

    async def send(self, session_id: str, text: str) -> None:
        pass

    async def interrupt(self, session_id: str) -> None:
        pass

    async def query(self, session_id: str) -> list[dict]:
        return []


def _action(
    kind: str,
    seq: int,
    outcome: str,
    *,
    delivery: str = "",
    reason: str = "",
    detail: dict | None = None,
    at: str = "",
) -> SteeringAction:
    return SteeringAction(
        command_id=f"cmd-{seq}",
        kind=kind,
        outcome=outcome,  # type: ignore[arg-type]
        sequence=seq,
        delivery=delivery,
        reason=reason,
        detail=detail or {},
        at=at or f"2026-09-23T10:00:{seq:02d}+00:00",
    )


def _command(
    seq: int,
    *,
    kind: str = "pause",
    status: str = "received",
    work_id: str = "wp-1",
    payload: dict | None = None,
) -> ControlCommand:
    return ControlCommand.model_validate(
        {
            "schema": "forge.proposal.control-command/1",
            "command_id": f"cmd-{seq}",
            "work_id": work_id,
            "sequence": seq,
            "kind": kind,
            "actor_ref": "human:op",
            "actor_origin": "server_authenticated_human",
            "idempotency_key": f"key-{seq}",
            "status": status,
            "payload": payload or {},
        }
    )


# -- the pure projection ---------------------------------------------------------


class TestCategorization:
    def test_every_category_has_a_ladder_word_that_proves_it(self):
        projection = {
            "received": "request_received",
            "authorized": "authorized",
            "dispatching": "effect_dispatched",
            "applied": "effect_dispatched",  # the coarse rung IS the intent rung
            "outcome_unknown": "effect_dispatched",  # dispatched, unproven
            "vendor_accepted": "effect_observed",
            "checkpointed": "checkpoint_committed",
        }
        for word, expected in projection.items():
            (entry,) = timeline_from_journal([_command(1, status=word).model_dump()])
            assert entry.category == expected, word
            assert entry.outcome == word  # the raw rung stays visible

    def test_an_applied_action_is_an_observed_effect_with_its_booking(self):
        (entry,) = timeline_from_journal(
            [
                _action(
                    "steer",
                    1,
                    "applied",
                    delivery="application_observed",
                    detail={"mailbox_status": "checkpointed"},
                )
            ]
        )

        assert entry.category == "effect_observed"
        assert entry.command_id == "cmd-1"
        assert entry.kind == "steer"
        assert "observed" in entry.line
        assert "checkpointed" in entry.line

    def test_an_applied_action_with_an_unconfirmed_booking_is_still_observed(self):
        (entry,) = timeline_from_journal(
            [_action("pause", 2, "applied", detail={"mailbox_status": "checkpoint failed: x"})]
        )

        assert entry.category == "effect_observed"
        assert "checkpoint failed" in entry.line

    def test_an_unproven_delivery_is_a_dispatch_claim_never_an_observation(self):
        (entry,) = timeline_from_journal(
            [_action("steer", 3, "delivery_unknown", delivery="outcome_unknown")]
        )

        assert entry.category == "effect_dispatched"
        assert "UNPROVEN" in entry.line

    def test_refusals_and_errors_are_evidence_not_effects(self):
        rows = [
            _action("amend", 1, "refused", reason="the human gate owns revisions"),
            _action("steer", 2, "ignored", reason="another run"),
            _action("steer", 3, "error", reason="vendor effect failed"),
        ]

        entries = timeline_from_journal(rows)

        assert [entry.category for entry in entries] == ["evidence_recorded"] * 3
        assert "human gate" in entries[0].line  # the reason rides the line
        assert "no effect claim" in entries[0].line

    def test_a_channel_error_row_is_evidence(self):
        row = {
            "type": "lane_control_error",
            "at": "2026-09-23T10:00:00+00:00",
            "error": "control-plane fetch failed: boom",
        }

        (entry,) = timeline_from_journal([row])

        assert entry.category == "evidence_recorded"
        assert "control-plane fetch failed" in entry.line
        assert entry.at == "2026-09-23T10:00:00+00:00"

    def test_a_durable_audit_entry_classifies_by_its_to_word(self):
        rows = [
            {"at": "2026-09-23T10:00:01+00:00", "to": "received"},
            {"at": "2026-09-23T10:00:02+00:00", "to": "authorized"},
            {"at": "2026-09-23T10:00:03+00:00", "to": "dispatching"},
            {"at": "2026-09-23T10:00:04+00:00", "to": "checkpointed"},
            {"at": "2026-09-23T10:00:05+00:00", "to": "expired"},
        ]

        entries = timeline_from_journal(rows)

        assert [entry.category for entry in entries] == [
            "request_received",
            "authorized",
            "effect_dispatched",
            "checkpoint_committed",
            "evidence_recorded",
        ]

    def test_an_ack_journal_row_classifies_by_its_state(self):
        row = {
            "source": "lane_channel",
            "state": "checkpointed",
            "at": "2026-09-23T10:00:09+00:00",
            "vendor_session_id": "sess-1",
        }

        (entry,) = timeline_from_journal([row])

        assert entry.category == "checkpoint_committed"

    def test_an_unknown_ladder_word_is_evidence_not_a_guess(self):
        row = {"command_id": "cmd-1", "kind": "pause", "status": "teleported"}

        (entry,) = timeline_from_journal([row])

        assert entry.category == "evidence_recorded"
        assert "teleported" in entry.line


class TestOrderingAndShape:
    def test_pause_then_steer_journal_keeps_its_order_and_categories(self):
        """The pinned scenario: an urgent pause (higher sequence) was applied
        BEFORE the queued steer — the timeline must say exactly that."""
        rows = [
            _action(
                "pause",
                2,
                "applied",
                delivery="application_observed",
                detail={"mailbox_status": "checkpointed"},
            ),
            _action(
                "steer",
                1,
                "applied",
                delivery="application_observed",
                detail={"mailbox_status": "checkpointed", "queued_for_resume": True},
            ),
        ]

        entries = timeline_from_journal(rows)

        assert [(entry.kind, entry.category) for entry in entries] == [
            ("pause", "effect_observed"),
            ("steer", "effect_observed"),
        ]
        # the order is the JOURNAL's (pause first), never re-sorted by sequence
        assert [entry.kind for entry in entries] == ["pause", "steer"]

    def test_a_mixed_journal_distinguishes_request_effect_and_evidence(self):
        """One pause's full journey: recorded → authorized → dispatched →
        observed (lane) → checkpointed — five rows, five categories."""
        rows = [
            {
                "at": "2026-09-23T10:00:01+00:00",
                "command_id": "cmd-9",
                "kind": "pause",
                "to": "received",
            },
            {
                "at": "2026-09-23T10:00:02+00:00",
                "command_id": "cmd-9",
                "kind": "pause",
                "state": "authorized",
            },
            {
                "at": "2026-09-23T10:00:03+00:00",
                "command_id": "cmd-9",
                "kind": "pause",
                "status": "dispatching",
            },
            _action("pause", 9, "applied", delivery="application_observed"),
            {
                "at": "2026-09-23T10:00:05+00:00",
                "command_id": "cmd-9",
                "kind": "pause",
                "state": "checkpointed",
            },
        ]

        entries = timeline_from_journal(rows)

        assert [entry.category for entry in entries] == [
            "request_received",
            "authorized",
            "effect_dispatched",
            "effect_observed",
            "checkpoint_committed",
        ]
        assert all(entry.command_id == "cmd-9" for entry in entries)
        assert all(entry.kind == "pause" for entry in entries)
        assert all(entry.line for entry in entries)  # human-readable, every one

    def test_every_category_name_is_in_the_closed_set(self):
        entries = timeline_from_journal(
            [
                _action("steer", 1, "applied"),
                _action("steer", 2, "refused"),
                _command(3, status="checkpointed").model_dump(),
            ]
        )
        assert {entry.category for entry in entries} <= set(TIMELINE_CATEGORIES)

    def test_the_entry_carries_timestamp_command_kind_outcome_and_line(self):
        (entry,) = timeline_from_journal(
            [_action("resume", 4, "applied", at="2026-09-23T11:22:33+00:00")]
        )

        assert entry.at == "2026-09-23T11:22:33+00:00"
        assert entry.command_id == "cmd-4"
        assert entry.kind == "resume"
        assert entry.outcome == "applied"
        assert isinstance(entry.line, str) and entry.line

    def test_rows_without_a_timestamp_carry_an_empty_at_not_a_fake_one(self):
        (entry,) = timeline_from_journal([_command(1, status="received").model_dump()])

        assert entry.at == ""
        assert entry.category == "request_received"


class TestHonestSkipping:
    def test_an_empty_journal_is_an_empty_timeline(self):
        assert timeline_from_journal([]) == []
        assert timeline_rows([]) == []

    def test_malformed_rows_are_skipped_not_fatal(self):
        rows: list[object] = [
            None,
            42,
            "not a row",
            b"bytes",
            object(),
            {},  # nothing classifiable
            {"nonsense": True},
            {"kind": "pause"},  # no outcome, no ladder word
        ]

        assert timeline_from_journal(rows) == []

    def test_malformed_rows_never_hide_their_valid_neighbors(self):
        rows: list[object] = [
            "garbage",
            _action("pause", 1, "applied"),
            {"nonsense": True},
            _command(2, kind="steer", status="checkpointed").model_dump(),
        ]

        entries = timeline_from_journal(rows)

        assert [entry.category for entry in entries] == [
            "effect_observed",
            "checkpoint_committed",
        ]

    def test_timeline_rows_are_json_safe_dicts(self):
        rows = timeline_rows(
            [
                _action("pause", 1, "applied"),
                {"type": "lane_control_error", "at": "t", "error": "e"},
            ]
        )

        assert all(isinstance(row, dict) for row in rows)
        assert rows[0] == asdict(timeline_from_journal([_action("pause", 1, "applied")])[0])
        assert set(rows[0]) == {"category", "at", "command_id", "kind", "outcome", "line"}
        assert all(isinstance(value, str) for row in rows for value in row.values())


# -- the session projection (the lane sidecar's source) ---------------------------


class TestSessionTimeline:
    async def test_the_session_projects_its_own_journal(self):
        svc = OperatorControlService()
        client = FakeClaudeClient()
        session = LaneSteeringSession(
            service=svc,
            driver=ClaudeSDKAdapter(client),
            driver_kind="claude",
            run_id="run-1",
            work_id="wp-1",
            vendor_session_id="sess-1",
        )
        await svc.submit(_command(1, kind="pause"))
        await svc.submit(_command(2, kind="steer", payload={"text": "tighten the retry bounds"}))

        await session.drain_once()

        entries = session.timeline

        assert [entry.kind for entry in entries] == ["pause", "steer"]  # interrupt-first
        assert [entry.category for entry in entries] == ["effect_observed", "effect_observed"]
        assert all(entry.at for entry in entries)  # every journaled action carries its time

    async def test_a_refused_command_journals_as_evidence_on_the_timeline(self):
        svc = OperatorControlService()
        session = LaneSteeringSession(
            service=svc,
            driver=ClaudeSDKAdapter(FakeClaudeClient()),
            driver_kind="claude",
            run_id="run-1",
            work_id="wp-1",
            vendor_session_id="sess-1",
        )
        await svc.submit(_command(1, kind="amend", payload={"text": "swap the broker"}))

        await session.drain_once()

        (entry,) = session.timeline
        assert entry.category == "evidence_recorded"
        assert entry.kind == "amend"


# -- the /status exposure (the router's timeline section) --------------------------


class TestRouterTimelineSection:
    async def test_a_recorded_command_without_a_lane_shows_as_request_received(self):
        """R28-29's acceptance: a recorded /resume is visibly NOT a scheduled
        runner — the timeline row says request_received until a lane drains."""
        svc = OperatorControlService()
        await svc.submit(_command(1, kind="resume").model_copy(update={"kind": "resume"}))

        rows = control_timeline(svc, "wp-1")

        assert rows is not None
        assert [row["category"] for row in rows] == ["request_received"]
        assert rows[0]["kind"] == "resume"
        assert "awaiting the lane" in rows[0]["line"]

    async def test_a_drained_command_climbs_to_checkpoint_committed(self):
        svc = OperatorControlService()
        session = LaneSteeringSession(
            service=svc,
            driver=ClaudeSDKAdapter(FakeClaudeClient()),
            driver_kind="claude",
            run_id="run-1",
            work_id="wp-1",
            vendor_session_id="sess-1",
        )
        await svc.submit(_command(1, kind="pause"))
        await session.drain_once()

        rows = control_timeline(svc, "wp-1")

        assert rows is not None
        assert [row["category"] for row in rows] == ["checkpoint_committed"]

    async def test_no_evidence_means_no_section(self):
        assert control_timeline(OperatorControlService(), "wp-1") is None

    async def test_another_works_evidence_is_not_this_works_section(self):
        svc = OperatorControlService()
        await svc.submit(_command(1, kind="pause").model_copy(update={"work_id": "wp-OTHER"}))

        assert control_timeline(svc, "wp-1") is None

    async def test_the_router_attaches_the_section_only_when_opted_in(self):
        """The result-dict attachment is OFF by default: the existing routing
        contract (its exact result dicts) is untouched until /status opts in."""
        import dataclasses as dc

        from forge.adaptive.command_router import ControlCommandRouter

        field = next(f for f in dc.fields(ControlCommandRouter) if f.name == "include_timeline")
        assert field.default is False

        async def post_note(body: str) -> None:
            return None

        svc = OperatorControlService()
        router = ControlCommandRouter(
            session_factory=None, settings=object(), post_note=post_note, control=svc
        )
        assert router.include_timeline is False

    def test_the_note_section_renders_one_line_per_entry(self):
        rows = timeline_rows(
            [
                {
                    "at": "2026-09-23T10:00:01+00:00",
                    "command_id": "cmd-1",
                    "kind": "pause",
                    "to": "received",
                },
                _action("pause", 1, "applied", at="2026-09-23T10:00:04+00:00"),
            ]
        )

        section = timeline_note_section(rows)

        assert section.startswith("**Control timeline:**")
        body = section.splitlines()[1:]
        assert len(body) == 2
        assert "10:00:01Z" in body[0]
        assert "`request_received`" in body[0]
        assert "`effect_observed`" in body[1]
