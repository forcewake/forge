"""R41-12 (#367) — ONE lineage view, acknowledgement-aware recovery.

The #364 live-loop shape as fixtures: delivery 1 (the ordinary parent,
oracle green) → round 2 (the /fix child, completed) → round 3 (a second
distinct correction, worker-killed post-commit, completed through the
#358 own-effect adoption) → round 4 (the conflicting-head negative arm:
a human commit landed mid-lane, the writer refused the typed
``branch_drift``, the child parked blocked, the round settled STALE and
the human commit stayed the branch head — preserved, never reverted).

Pinned here over that shape:

- the ONE lineage snapshot (:func:`lineage_view`): the human round
  reference (``round 4 of 7319478e``), the delivery history, the
  persisted #359 collaboration target, the candidate's role, the exact
  checkpoint, the native occupancy word (#360 — unknown never renders
  stopped), the budget axes WITH the amendment history and the
  unresolved effects — each fact from its canonical rows;
- the COMMAND-STATE ladder (received / durably accepted / dispatched /
  applied / checkpointed): five DIFFERENT durable rows distinguish the
  rungs, and a pause at a comment ACK never renders as a successful
  pause;
- the action versioning: the FULL subject (run id + round reference +
  round status), the server-side reauthorization refusing a stale
  action (the #349 ``operator.stale_action_refusal`` extended to the
  round-STATUS fence), and printed commands that resolve through the
  FULL run id (never a shared historical prefix);
- the explicit operator prerequisites (stale head, missing checkpoint,
  expired credential — the #364 live findings);
- the ONE concise MR status comment and the credential-free lineage
  support export (secrets, raw briefs and checkpoint contents omitted by
  default — sentinel-tested, redactions counted);
- THE BLINDED-OPERATOR WALK (AC-7): a second operator who sees ONLY the
  rendered documents recovers the seeded round-4 failure end-to-end
  through the view's own offered runbook action, stale replay refused
  first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forge.adaptive.operator_view import (
    COMMAND_STATES,
    COMMAND_STATE_PROOF,
    LINEAGE_VIEW_SCHEMA,
    OPERATOR_VIEW_SCHEMA,
    STALE_ACTION_REFUSAL,
    OperatorAction,
    RecoveryActions,
    command_state_rows,
    export_lineage_support,
    initial_projection,
    lineage_prerequisites,
    lineage_reference,
    lineage_view,
    render_lineage_comment,
)
from forge.durable.collaboration import round_reference as durable_round_reference

NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)

# -- the #364 qualifying lineage (disposable project 160, issue #5, MR !4,
# branch factory/5/7319478e) — the run ids, candidates and commits are the
# trace's own identities, extended to full 32-hex run ids.
ROOT = "7319478e" + "1" * 24
R2_CHILD = "bbd8d0d4" + "2" * 24
R3_CHILD = "17645ea9" + "3" * 24
R4_CHILD = "2396987b" + "4" * 24
R5_CHILD = "0aa41c62" + "5" * 24
CAND_ROOT = "6e973202" + "a" * 32
CAND_R2 = "f72223ce" + "b" * 32
COMMIT_R3 = "7008c940" + "c" * 32
HUMAN_COMMIT = "f9e17e25" + "d" * 32  # the preserved human commit (round 4)
BRANCH = "factory/5/7319478e"


def _run(run_id: str = ROOT, **over) -> dict:
    row: dict = {
        "id": run_id,
        "status": "ready_for_human",
        "base_sha": "b" * 40,
        "candidate_shas": [CAND_ROOT],
        "plan_digest": "p" * 64,
        "evidence": {},
        "blocked_reason": "",
        "cancel_requested": False,
        "created_at": "2026-09-27T08:00:00+00:00",
        "updated_at": "2026-09-27T11:00:00+00:00",
    }
    row.update(over)
    return row


def _round(number: int, status: str, *, child: str, parent: str, **over) -> dict:
    row: dict = {
        "round_id": f"rr-{number}",
        "round_number": number,
        "status": status,
        "status_reason": "",
        "decision_id": f"dec-{number}",
        "note_id": str(1500 + number),
        "mr_iid": 4,
        "base_head_sha": "",
        "parent_run_id": parent,
        "child_run_id": child,
        "root_run_id": ROOT,
        "requested_by": "reviewer:op",
        "created_at": f"2026-09-27T0{number}:00:00+00:00",
        "updated_at": f"2026-09-27T0{number}:30:00+00:00",
    }
    row.update(over)
    return row


def _target(**over) -> dict:
    row: dict = {
        "id": "tgt-1",
        "provider": "gitlab",
        "project_ref": "160",
        "root_run_id": ROOT,
        "source_branch": BRANCH,
        "target_branch": "main",
        "mr_iid": 4,
        "status": "active",
        "refusal_reason": "",
        "provenance": "live",
    }
    row.update(over)
    return row


def _budget(**over) -> dict:
    row: dict = {
        "budget_id": "bud-1",
        "status": "open",
        "max_calls": 40,
        "max_tokens": 600_000,
        "wallclock_s": 3600,
        "reserved_calls": 0,
        "reserved_tokens": 0,
        "consumed_calls": 12,
        "consumed_tokens": 240_000,
        "unresolved_calls": 0,
        "unresolved_tokens": 0,
        "closing_partition_policy": "closing-partition/1",
        "closing_reserved_calls": 4,
        "closing_reserved_tokens": 60_000,
    }
    row.update(over)
    return row


def _amendment(axis: str = "calls", status: str = "applied", **over) -> dict:
    amounts = {
        "usd": {"amount_usd": 1.5},
        "calls": {"amount_calls": 10},
        "tokens": {"amount_tokens": 100_000},
        "wallclock": {"amount_wallclock_s": 900},
    }
    row: dict = {
        "command_id": "run:continue_review:160:918",
        "axis": axis,
        "status": status,
        "refusal_reason": "",
        "limit_before": {"calls": 40},
        "limit_after": {"calls": 50},
        "applied_at": "2026-09-27T11:40:00+00:00",
    }
    row.update(amounts[axis])
    row.update(over)
    return row


def _command(seq: int, kind: str, status: str, **over) -> dict:
    row: dict = {
        "command_id": f"cmd-{seq}",
        "work_id": ROOT,
        "sequence": seq,
        "kind": kind,
        "status": status,
        "actor_ref": "human:op",
        "created_at": "2026-09-27T11:45:00+00:00",
    }
    row.update(over)
    return row


def _verification(result: str = "passed", candidate: str = CAND_ROOT, **over) -> dict:
    row: dict = {
        "verification_id": "ver-1",
        "result": result,
        "candidate_sha": candidate,
        "producer": "gitlab-pipeline",
        "at": "2026-09-27T10:30:00+00:00",
    }
    row.update(over)
    return row


def _lease(occupancy_word: str = "dispatched_unknown", **over) -> dict:
    # the operator occupancy row shape (the reader's derived word rides
    # ``occupancy``); the pure view never derives it from raw columns.
    row: dict = {
        "lease_id": "lease-1",
        "project_id": 160,
        "provider": "gitlab",
        "slot": 1,
        "occupancy": occupancy_word,
        "acquired_at": "2026-09-27T11:50:00+00:00",
        "released_at": "",
        "native_intent_ref": f"gitlab:pipeline:160@{BRANCH}",
        "native_handle": "",
    }
    row.update(over)
    return row


def _rounds_through_three() -> list[dict]:
    return [
        _round(2, "completed", child=R2_CHILD, parent=ROOT, base_head_sha=CAND_ROOT),
        _round(3, "completed", child=R3_CHILD, parent=R2_CHILD, base_head_sha=CAND_R2),
    ]


def _blocked_round4_rows(**sections) -> dict:
    """The #364 negative arm as ONE snapshot: rounds 2/3 completed, round
    4 STALE — the human commit f9e17e25 landed mid-lane, the writer
    refused the typed branch_drift, the round settled and the human
    commit stayed the head."""
    rows: dict = {
        "run": _run(),
        "rounds": _rounds_through_three()
        + [
            _round(
                4,
                "stale",
                child=R4_CHILD,
                parent=R3_CHILD,
                base_head_sha=COMMIT_R3,
                status_reason="the MR head moved off the approved base",
            )
        ],
        "target": _target(),
        "verifications": [_verification()],
        "budget": _budget(),
        "amendments": [_amendment()],
        "commands": [_command(1, "pause", "vendor_accepted")],
        "inbox": [
            {
                "source_event_id": "inb-fix-1519",
                "event_type": "run_command",
                "command": "fix",
                "note_id": "1519",
                "command_ref": "",
                "status": "processed",
                "received_at": "2026-09-27T11:00:00+00:00",
                "processed_at": "2026-09-27T11:01:00+00:00",
            }
        ],
    }
    rows.update(sections)
    return rows


OCCUPANCY_UNKNOWN = [_lease("dispatched_unknown")]


# ---------------------------------------------------------------------------
# The lineage reference and the ONE snapshot shape
# ---------------------------------------------------------------------------


class TestLineageReference:
    def test_rounds_two_and_up_match_the_durable_spelling(self):
        for number in (2, 3, 4, 5):
            assert lineage_reference(number, ROOT) == durable_round_reference(number, ROOT), (
                "the lineage view's human reference must spell exactly what #359 persists"
            )

    def test_delivery_one_is_named_as_the_root_delivery(self):
        assert lineage_reference(1, ROOT) == "delivery 1 of 7319478e"
        assert lineage_reference(0, ROOT) == "delivery 1 of 7319478e"
        assert lineage_reference(None, ROOT) == "delivery 1 of 7319478e"


class TestOneLineageSnapshot:
    def test_the_snapshot_names_the_current_round_after_two_corrections(self):
        """AC: after two corrections the view unambiguously names the
        current round and the historical deliveries."""
        rows = _blocked_round4_rows()
        view = lineage_view(rows, now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        assert view["schema"] == LINEAGE_VIEW_SCHEMA
        lineage = view["lineage"]
        assert lineage["root_run_id"] == ROOT
        # the human reference names round 4 — the newest delivery — with
        # the root's 8-char display prefix, exactly the #359 convention
        assert lineage["reference"] == "round 4 of 7319478e"
        # the delivery HISTORY: delivery 1 + rounds 2..4, each with its
        # own reference, status and child run — never a UUID-prefix guess
        numbers = [entry["round_number"] for entry in lineage["deliveries"]]
        assert numbers == [1, 2, 3, 4]
        by_number = {entry["round_number"]: entry for entry in lineage["deliveries"]}
        assert by_number[1]["reference"] == "delivery 1 of 7319478e"
        assert by_number[2]["status"] == "completed"
        assert by_number[3]["status"] == "completed"
        assert by_number[4]["status"] == "stale"
        assert by_number[4]["run_id"] == R4_CHILD
        # the current round carries the machine ref AND the status the
        # action CAS names; the slot is free (round 4 settled stale)
        current = lineage["current"]
        assert current["reference"] == "round 4 of 7319478e"
        assert current["round_ref"] == "round:4:dec-4"
        assert current["round_status"] == "stale"
        assert current["open"] is False
        assert current["active_child_run_id"] == ""

    def test_before_round_four_the_newest_completed_round_is_current(self):
        rows = _blocked_round4_rows()
        rows["rounds"] = _rounds_through_three()
        view = lineage_view(rows, now=NOW)
        assert view["lineage"]["reference"] == "round 3 of 7319478e"
        assert view["lineage"]["rounds_recorded"] == 2  # the table rows; delivery 1 is the run

    def test_the_collaboration_target_is_the_persisted_record(self):
        view = lineage_view(_blocked_round4_rows(), now=NOW)
        target = view["target"]
        assert target["source_branch"] == BRANCH
        assert target["target_branch"] == "main"
        assert target["mr_iid"] == 4
        assert target["status"] == "active"
        assert target["available"] is True
        assert target["evidence"]["of"] == "collaboration target"

    def test_a_refused_legacy_target_renders_its_refusal_never_a_branch(self):
        rows = _blocked_round4_rows(
            target=_target(
                status="refused",
                source_branch="",
                target_branch="",
                refusal_reason="collaboration_target_unresolved: no recorded MR or branch",
            )
        )
        view = lineage_view(rows, now=NOW)
        target = view["target"]
        assert target["available"] is False
        assert target["source_branch"] == ""
        assert "unresolved" in target["refusal_reason"]

    def test_the_candidate_is_historical_after_any_superseding_round(self):
        """The root's delivery was superseded by round 2 — however many
        further rounds followed, its candidate and its green evidence
        are HISTORY, and the lineage's current candidate lives on the
        open round's child run."""
        view = lineage_view(_blocked_round4_rows(), now=NOW)
        candidate = view["candidate"]
        assert candidate["sha"] == CAND_ROOT
        assert candidate["role"] == "historical"
        assert candidate["superseded_by"] == "round:3:dec-3"
        assert candidate["lineage_current_run"] == ROOT  # no open round → this run
        assert "HISTORY" in candidate["note"]
        assert view["verification"]["binding"] == "historical"

    def test_an_open_round_names_its_child_as_the_active_execution(self):
        rows = _blocked_round4_rows()
        rows["rounds"] = _rounds_through_three() + [
            _round(5, "admitted", child=R5_CHILD, parent=R3_CHILD, base_head_sha=HUMAN_COMMIT)
        ]
        view = lineage_view(rows, now=NOW)
        current = view["lineage"]["current"]
        assert current["reference"] == "round 5 of 7319478e"
        assert current["open"] is True
        assert current["active_child_run_id"] == R5_CHILD
        assert candidate_run(view) == R5_CHILD

    def test_a_delayed_parent_event_cannot_change_the_displayed_active_child(self):
        """AC: a delayed parent event (round 2's row arriving LATE, with a
        newer updated_at and even re-dispatched) cannot displace the
        displayed active child — the fold orders by ROUND NUMBER and the
        active child keys on the OPEN round, never on recency."""
        rows = _blocked_round4_rows()
        rows["rounds"] = _rounds_through_three() + [
            _round(4, "admitted", child=R4_CHILD, parent=R3_CHILD, base_head_sha=COMMIT_R3)
        ]
        # the delayed parent event: round 2 re-observed AFTER round 4's
        # row landed (newest updated_at in the whole section)
        delayed = _round(
            2,
            "dispatched",
            child=R2_CHILD,
            parent=ROOT,
            updated_at="2026-09-27T11:59:00+00:00",
        )
        rows["rounds"] = [delayed] + [row for row in rows["rounds"] if row["round_number"] != 2]
        view = lineage_view(rows, now=NOW)
        current = view["lineage"]["current"]
        assert current["reference"] == "round 4 of 7319478e"
        assert current["active_child_run_id"] == R4_CHILD
        assert [entry["round_number"] for entry in view["lineage"]["deliveries"]] == [1, 2, 3, 4]

    def test_the_budget_axes_carry_their_amendment_history(self):
        rows = _blocked_round4_rows(
            amendments=[
                _amendment(),
                _amendment(
                    axis="tokens",
                    status="refused",
                    amount_tokens=50_000,
                    refusal_reason="tokens: the axis is not limiting",
                    command_id="run:continue_review:160:919",
                ),
            ]
        )
        view = lineage_view(rows, now=NOW)
        budget = view["budget"]
        assert budget["status"] == "open"
        assert budget["axes"]["calls"] == {
            "limit": 40,
            "reserved": 0,
            "consumed": 12,
            "unresolved": 0,
        }
        assert budget["axes"]["tokens"]["limit"] == 600_000
        assert budget["closing_partition"]["policy"] == "closing-partition/1"
        assert len(budget["amendments"]) == 2
        applied = next(row for row in budget["amendments"] if row["status"] == "applied")
        assert applied["axis"] == "calls"
        assert applied["amount"] == 10
        assert applied["limit_after"] == {"calls": 50}
        refused = next(row for row in budget["amendments"] if row["status"] == "refused")
        assert refused["amount"] == 50_000
        assert refused["refusal_reason"].startswith("tokens:")
        assert budget["amendments_refused"] == 1

    def test_an_unqueried_budget_renders_unknown_never_a_zeroed_ledger(self):
        rows = _blocked_round4_rows()
        rows.pop("budget")
        rows.pop("amendments")
        view = lineage_view(rows, now=NOW)
        assert view["budget"]["status"] == "unknown"
        assert view["budget"]["amendments"] == []

    def test_unresolved_effects_ride_the_lineage_snapshot(self):
        rows = _blocked_round4_rows(
            publications=[
                {
                    "operation_key": "op-77",
                    "status": "dispatched",
                    "operation": "commit",
                    "target_ref": BRANCH,
                    "at": "2026-09-27T11:55:00+00:00",
                }
            ]
        )
        view = lineage_view(rows, now=NOW)
        assert [effect["operation_key"] for effect in view["unresolved_effects"]] == ["op-77"]


def candidate_run(view: dict) -> str:
    return str(view["candidate"]["lineage_current_run"])


def _by_code(prerequisites: list[dict], code: str) -> dict | None:
    return next((row for row in prerequisites if row["code"] == code), None)


# ---------------------------------------------------------------------------
# The command-state ladder — which durable row distinguishes each state
# ---------------------------------------------------------------------------


class TestCommandStates:
    def test_five_durable_rows_distinguish_the_five_rungs(self):
        """AC: received / durably accepted / dispatched / applied /
        checkpointed are DIFFERENT states proven by DIFFERENT rows (the
        #357 durable-acceptance ladder)."""
        rows = {
            "run": _run(),
            "inbox": [
                {
                    "source_event_id": "inb-1",
                    "event_type": "run_command",
                    "command": "fix",
                    "note_id": "1601",
                    "command_ref": "",
                    "status": "pending",
                    "received_at": "2026-09-27T11:58:00+00:00",
                }
            ],
            "commands": [
                _command(1, "pause", "received"),
                _command(2, "pause", "vendor_accepted"),
                _command(3, "steer", "applied", applied_at="2026-09-27T11:50:00+00:00"),
                _command(4, "pause", "checkpointed", applied_at="2026-09-27T11:52:00+00:00"),
                _command(5, "resume", "rejected"),
            ],
            "checkpoints": [
                {
                    "checkpoint_id": "ck-1",
                    "digest": "d" * 64,
                    "committed_at": "2026-09-27T11:52:00+00:00",
                    "fence": "held",
                }
            ],
        }
        entries = command_state_rows(rows)
        by_id = {entry["command_id"]: entry for entry in entries}
        # received — the INBOX row alone (the delivery landed, nothing
        # durably accepted it)
        inbox_entry = next(entry for entry in entries if entry["command_state"] == "received")
        assert inbox_entry["proven_by"]["of"] == "event inbox"
        # durably accepted — the CONTROL COMMAND row (the 202 contract)
        assert by_id["cmd-1"]["command_state"] == "durably_accepted"
        assert by_id["cmd-1"]["proven_by"]["of"] == "control command"
        # dispatched — the dispatch rung (the vendor may have TAKEN it —
        # a comment ack is NOT a safe state)
        assert by_id["cmd-2"]["command_state"] == "dispatched"
        assert "vendor_accepted" in by_id["cmd-2"]["rung"]
        # applied — the lane's application ack
        assert by_id["cmd-3"]["command_state"] == "applied"
        # checkpointed — the rung PLUS the committed checkpoint row
        assert by_id["cmd-4"]["command_state"] == "checkpointed"
        assert rows["checkpoints"][0]["committed_at"]
        # refused — spent without applying
        assert by_id["cmd-5"]["command_state"] == "refused"
        # every state's proof sentence names a durable row kind
        assert set(COMMAND_STATE_PROOF) == set(COMMAND_STATES)

    def test_a_pause_is_not_successful_because_a_comment_was_accepted(self):
        """The review's core ask: the honest rendering of a pause at a
        comment ACK — dispatched, NOT successful, until its checkpoint
        commits."""
        for rung in (
            "received",
            "authorized",
            "dispatching",
            "vendor_accepted",
            "outcome_unknown",
            "applied",
        ):
            rows = {"run": _run(), "commands": [_command(1, "pause", rung)]}
            (entry,) = command_state_rows(rows)
            assert entry["command_state"] in ("durably_accepted", "dispatched", "applied")
            assert "NOT successful" in entry["honest_note"], rung
        rows = {"run": _run(), "commands": [_command(1, "pause", "checkpointed")]}
        (entry,) = command_state_rows(rows)
        assert entry["command_state"] == "checkpointed"
        assert "NOT successful" not in entry["honest_note"]

    def test_a_non_pause_command_never_carries_the_pause_note(self):
        rows = {"run": _run(), "commands": [_command(1, "steer", "vendor_accepted")]}
        (entry,) = command_state_rows(rows)
        assert entry["command_state"] == "dispatched"
        assert "NOT successful" not in entry["honest_note"]

    def test_an_inbox_row_referencing_a_command_is_its_receipt_not_a_new_entry(self):
        rows = {
            "run": _run(),
            "commands": [_command(1, "pause", "received")],
            "inbox": [
                {
                    "source_event_id": "inb-1",
                    "command": "pause",
                    "command_ref": "cmd-1",
                    "status": "processed",
                    "received_at": "2026-09-27T11:44:00+00:00",
                }
            ],
        }
        entries = command_state_rows(rows)
        assert len(entries) == 1
        assert entries[0]["command_state"] == "durably_accepted"

    def test_an_unobserved_command_authority_renders_one_honest_unknown(self):
        (entry,) = command_state_rows({"run": _run()})
        assert entry["command_state"] == "unknown"
        assert entry["proven_by"] is None


# ---------------------------------------------------------------------------
# Occupancy and verification honesty (#360's rule on this surface)
# ---------------------------------------------------------------------------


class TestHonestOccupancyAndVerification:
    @pytest.mark.parametrize(
        ("lease_word", "expected"),
        [
            ("dispatched_unknown", "unknown"),
            ("draining", "unknown"),
            ("never_dispatched", "unknown"),
            ("native_running", "running"),
        ],
    )
    def test_unproven_occupancy_never_renders_stopped(self, lease_word, expected):
        view = lineage_view(_blocked_round4_rows(), now=NOW, occupancy=[_lease(lease_word)])
        assert view["occupancy"]["native"] == expected
        assert view["occupancy"]["native"] != "terminal"

    def test_terminal_requires_an_observed_release(self):
        view = lineage_view(
            _blocked_round4_rows(),
            now=NOW,
            occupancy=[_lease("observed_terminal", released_at="2026-09-27T11:55:00+00:00")],
        )
        assert view["occupancy"]["native"] == "terminal"

    def test_an_unqueried_occupancy_authority_stays_unknown(self):
        view = lineage_view(_blocked_round4_rows(), now=NOW, occupancy=None)
        assert view["occupancy"]["native"] == "unknown"
        assert "never stopped" in view["occupancy"]["basis"]

    def test_unavailable_verification_never_renders_passed(self):
        rows = _blocked_round4_rows()
        rows.pop("verifications")
        view = lineage_view(rows, now=NOW, coverage={"verifications": "unknown"})
        assert view["verification"]["verdict"] == "unknown"
        assert view["verification"]["binding"] == "unknown"

    def test_a_stale_or_inconsistent_projection_flags_itself(self):
        view = lineage_view(_blocked_round4_rows(), now=NOW, projection_inconsistent=True)
        assert view["stale_or_inconsistent"] is True
        assert "uncertainty" in view


# ---------------------------------------------------------------------------
# Action versioning — the full subject and the round-status fence
# ---------------------------------------------------------------------------


class TestActionVersioning:
    def test_actions_name_the_full_subject_and_print_resolving_commands(self):
        """AC: the printed command resolves correctly with shared
        historical prefixes — it names the FULL run id (#359 kept short
        prefixes display-only), and the subject is run + round ref."""
        view = lineage_view(_blocked_round4_rows(), now=NOW)
        actions = view["actions"]
        assert actions
        for action in actions:
            assert action["subject"] == f"{ROOT}@round:4:dec-4"
            # the printed command carries the COMPLETE run id — never an
            # 8-char prefix that round ids share historically
            assert ROOT in action["command"]
            assert action["expected_round_status"] == "stale"

    def test_decide_refuses_a_stale_action_through_the_round_status_fence(self):
        """The #349 stale_action_refusal extended (#367): an action
        planned while round 4 was OPEN replays after the settle — same
        version, same candidate, same round REFERENCE — and is refused
        because the round's STATUS moved."""
        settled_rows = _blocked_round4_rows()
        open_rows = _blocked_round4_rows()
        open_rows["rounds"] = _rounds_through_three() + [
            _round(4, "dispatched", child=R4_CHILD, parent=R3_CHILD, base_head_sha=COMMIT_R3)
        ]
        then = initial_projection(open_rows, NOW)
        assert then.round_status == "dispatched"
        planned = next(
            action
            for action in RecoveryActions.plan(then, "op@example", "approver", at=NOW)
            if action.action == "probe"
        )
        now_projection = initial_projection(settled_rows, NOW)
        assert now_projection.round_status == "stale"
        decision = RecoveryActions.decide(planned, now_projection)
        assert decision.allowed is False
        assert STALE_ACTION_REFUSAL in decision.reason
        assert "'dispatched'" in decision.reason and "'stale'" in decision.reason
        assert decision.current_state == now_projection.state
        assert decision.safe_next_action

    def test_decide_accepts_the_fresh_action_against_the_same_world(self):
        rows = _blocked_round4_rows()
        projection = initial_projection(rows, NOW)
        planned = next(
            action
            for action in RecoveryActions.plan(projection, "op@example", "approver", at=NOW)
            if action.action == "probe"
        )
        decision = RecoveryActions.decide(planned, initial_projection(rows, NOW))
        assert decision.allowed is True

    def test_planned_actions_carry_the_subject_and_status_ticket(self):
        projection = initial_projection(_blocked_round4_rows(), NOW)
        planned = next(
            action
            for action in RecoveryActions.plan(projection, "op@example", "approver", at=NOW)
            if action.action == "probe"
        )
        assert planned.subject == f"{ROOT}@round:4:dec-4"
        assert planned.expected_round_status == "stale"
        fact = planned.audit_fact()
        assert fact["subject"] == planned.subject
        assert fact["expected_round_status"] == "stale"

    def test_a_directly_built_action_without_a_status_ticket_still_decides(self):
        """Backwards compatibility: the #349 action shape (no status
        ticket) decides exactly as before — the fence only bites when the
        ticket names a status."""
        rows = _blocked_round4_rows()
        projection = initial_projection(rows, NOW)
        legacy = OperatorAction(
            action="probe",
            state=projection.state,
            actor_role="observer",
            actor="op@example",
            digest=projection.action_digest,
            at="2026-09-27T12:00:00+00:00",
            linkage=f"run:{ROOT}",
            expected_version=projection.projection_version,
            via="read-only:/status",
            expected_candidate=str(projection.identity.get("active_candidate") or ""),
            expected_round=projection.round_ref,
        )
        decision = RecoveryActions.decide(legacy, projection)
        assert decision.allowed is True


# ---------------------------------------------------------------------------
# The explicit operator prerequisites (#364's live findings)
# ---------------------------------------------------------------------------


class TestPrerequisites:
    def test_the_stale_head_names_the_re_raise_route_and_the_preserved_head(self):
        rows = _blocked_round4_rows()
        prerequisites = lineage_prerequisites(rows)
        (entry,) = [row for row in prerequisites if row["code"] == "stale_head"]
        assert entry["available"] is True
        assert entry["via"] == "native-note:/fix"
        assert "human commits are preserved" in entry["description"]
        assert "re-raise the correction against the CURRENT head" in entry["description"]
        assert entry["evidence"]["of"] == "review round"

    def test_the_branch_drift_wording_alone_names_the_stale_head(self):
        rows = _blocked_round4_rows()
        rows["rounds"] = []  # no round rows — the run's own blocked reason
        rows["run"] = _run(
            status="blocked",
            blocked_reason=(
                "branch_drift: factory/5/7319478e moved away from the intent parent — "
                "human commits preserved"
            ),
        )
        assert _by_code(lineage_prerequisites(rows), "stale_head") is not None

    def test_the_expired_credential_names_the_rotation_runbook_never_retry(self):
        rows = _blocked_round4_rows()
        rows["run"] = _run(
            status="blocked",
            blocked_reason="runner redemption answered 401 token-expired for the broker credential",
        )
        entry = _by_code(lineage_prerequisites(rows), "expired_credential")
        assert entry is not None
        assert entry["available"] is False
        assert entry["via"] == "runbook:token-rotation"

    def test_the_missing_checkpoint_names_restore_or_retire_before_any_resume(self):
        rows = _blocked_round4_rows(
            checkpoints=[
                {
                    "checkpoint_id": "ck-1",
                    "digest": "d" * 64,
                    "committed_at": "2026-09-27T11:30:00+00:00",
                    "fence": "held",
                }
            ]
        )
        entry = _by_code(
            lineage_prerequisites(rows, coverage={"checkpoints": "missing"}), "missing_checkpoint"
        )
        assert entry is not None
        assert entry["available"] is False
        assert entry["via"] == "runbook:backup-restore"

    def test_an_healthy_lineage_renders_no_prerequisites(self):
        assert lineage_prerequisites({"run": _run()}) == []


# ---------------------------------------------------------------------------
# The MR status comment and the credential-free support export
# ---------------------------------------------------------------------------


class TestMrCommentAndSupportExport:
    def test_the_mr_comment_is_one_concise_block_with_the_marker(self):
        comment = render_lineage_comment(
            _blocked_round4_rows(), now=NOW, occupancy=OCCUPANCY_UNKNOWN
        )
        lines = comment.splitlines()
        assert lines[0] == "**Forge lineage — round 4 of 7319478e** (root `7319478e`)"
        assert any("`factory/5/7319478e` → `main`, MR !4" in line for line in lines)
        assert any("pause is dispatched — NOT successful yet" in line for line in lines)
        assert any("Occupancy:** unknown" in line for line in lines)
        assert any("stale_head (available)" in line for line in lines)
        assert lines[-1].startswith(f"<!-- forge-status:1 run={ROOT} identity=")
        assert "lineage=round 4 of 7319478e" in lines[-1]
        assert len(lines) <= 12  # concise — one screen, no event spam

    def test_the_comment_identity_is_stable_for_the_same_world(self):
        rows = _blocked_round4_rows()
        first = render_lineage_comment(rows, now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        second = render_lineage_comment(rows, now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        assert first == second  # replayed delivery collapses to ONE status

    def test_the_support_export_omits_secrets_raw_briefs_and_checkpoint_contents(self):
        """AC (sentinel-tested): secrets, raw reviewer briefs and
        checkpoint CONTENTS never ride the export — dropped by the
        allowlist by construction, and the redaction count states what
        the guard caught anyway."""
        rows = _blocked_round4_rows()
        # the sentinels a careless export would leak
        rows["run"] = _run(
            blocked_reason="401 unauthorized: the broker credential was rejected (glpat-xyz987654321)"
        )
        rows["verifications"] = []
        rows["checkpoints"] = [
            {
                "checkpoint_id": "ck-1",
                "digest": "d" * 64,
                "committed_at": "2026-09-27T11:30:00+00:00",
                "fence": "held",
                "contents": "SECRET PLAN TEXT AND RAW BRIEF: sk-live-abcdef123456",
                "brief": "the reviewer's raw brief text",
            }
        ]
        rows["amendments"] = [_amendment(reason="RAW OPERATOR TEXT with glpat-xyz987654321 inside")]
        export = export_lineage_support(rows, now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        rendered = repr(export)
        assert export["schema"] == "forge.operator.lineage-support/1"
        # secrets never ride
        assert "glpat-" not in rendered
        assert "sk-live-" not in rendered
        # raw briefs and checkpoint contents never ride
        assert "RAW OPERATOR TEXT" not in rendered
        assert "reviewer's raw brief" not in rendered
        assert "SECRET PLAN TEXT" not in rendered
        assert "brief" not in export["sections"]["checkpoint"]
        # the amendment rows carry the audit fields only (no free-text reason)
        assert set(export["sections"]["budget"]["amendments"][0]) >= {
            "command_id",
            "axis",
            "status",
        }
        assert "reason" not in export["sections"]["budget"]["amendments"][0]
        # the observability: the guard's redactions are counted
        assert export["export"]["redactions"] >= 1

    def test_the_support_export_stays_allowlisted_and_bounded(self):
        rows = _blocked_round4_rows(
            commands=[
                _command(seq, "steer", "applied", applied_at="2026-09-27T11:50:00+00:00")
                for seq in range(1, 30)
            ]
        )
        export = export_lineage_support(rows, now=NOW)
        assert (
            len(export["sections"]["command_states"]) == export["export"]["max_entries_per_section"]
        )
        # every serialized section field is declared
        from forge.adaptive.operator_view import LINEAGE_SUPPORT_FIELDS

        for name, fields in LINEAGE_SUPPORT_FIELDS.items():
            section = export["sections"].get(name)
            if isinstance(section, dict):
                assert set(section) <= set(fields), name
            elif isinstance(section, list):
                for entry in section:
                    assert set(entry) <= set(fields), name


# ---------------------------------------------------------------------------
# THE BLINDED-OPERATOR WALK (AC-7) — recovery using ONLY the view + the
# documented runbook action, end-to-end
# ---------------------------------------------------------------------------


class BlindedOperator:
    """The second operator: sees ONLY rendered documents (never the raw
    rows), decides from the view's own offered actions."""

    def __init__(self) -> None:
        self.observed: list[dict] = []

    def observe(self, document: dict) -> dict:
        self.observed.append(document)
        return document

    def read_current(self, document: dict) -> dict:
        return document["lineage"]["current"]

    def stale_head_action(self, document: dict) -> dict:
        """The runbook action the view offers for a stale head: re-raise
        the correction against the CURRENT head (a /fix note on the MR)."""
        offered = [
            row
            for row in document["prerequisites"]
            if row["code"] == "stale_head" and row["available"]
        ]
        assert offered, "the view must offer the stale-head recovery before the walk proceeds"
        (action,) = offered
        assert action["via"] == "native-note:/fix"
        return action


def _guarded_re_raise(world: dict, *, note_id: str) -> dict:
    """The documented runbook action's guarded apply — the test double of
    the runs service's native /fix admission. Server-side contract:

    - the lineage's outstanding correction slot must be FREE (no open
      round — the one-outstanding invariant);
    - the new round's approved base is the EXACT CURRENT MR head (the
        preserved human commit becomes ``base_head_sha`` — preserved,
        never reset, never force-pushed);
    - the round number is the lineage's next;
    - the parent is the newest COMPLETED delivery's child run.

    Refuses (typed) when the slot is taken — exactly like the real
    admission's ``conflicting_correction`` refusal.
    """
    rows = world["rows"]
    rounds = rows["rounds"]
    open_rounds = [row for row in rounds if row["status"] in ("admitted", "dispatched")]
    if open_rounds:
        return {
            "admitted": False,
            "refusal": "conflicting_correction: the open round holds the slot",
        }
    newest_number = max(row["round_number"] for row in rounds)
    completed = [row for row in rounds if row["status"] == "completed"]
    parent = completed[-1]["child_run_id"]
    child = "0aa41c62" + "5" * 24
    head = world["head"]
    rows["rounds"] = rounds + [
        _round(
            newest_number + 1,
            "admitted",
            child=child,
            parent=parent,
            base_head_sha=head,
            note_id=note_id,
        )
    ]
    rows["run"] = dict(rows["run"], updated_at="2026-09-27T12:10:00+00:00")
    return {"admitted": True, "round_number": newest_number + 1, "child_run_id": child}


class TestTheBlindedOperatorWalk:
    def test_a_blinded_operator_recovers_the_seeded_round4_failure(self):
        """AC-7: a second operator, seeing ONLY the view + the runbook,
        recovers the seeded ordinary failure (a round blocked on a moved
        head with the human commit preserved) end-to-end — walking the
        view's own offered actions, stale replay refused first."""
        # -- the seeded failure: the #364 round-4 world, the human commit
        #    f9e17e25 the current MR head (preserved, never reverted)
        world = {"rows": _blocked_round4_rows(), "head": HUMAN_COMMIT}
        operator = BlindedOperator()

        # 1. the operator reads ONLY the rendered lineage document
        document = operator.observe(
            lineage_view(world["rows"], now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        )
        assert document["schema"] == LINEAGE_VIEW_SCHEMA
        current = operator.read_current(document)
        assert current["reference"] == "round 4 of 7319478e"
        assert current["round_status"] == "stale"
        assert current["open"] is False
        # the view names the failure honestly: occupancy unknown (never
        # stopped), the pause dispatched (not successful), the stale head
        assert document["occupancy"]["native"] == "unknown"
        assert any(
            entry["kind"] == "pause" and entry["command_state"] == "dispatched"
            for entry in document["command_states"]
        )

        # 2. the offered runbook action is present and available
        action = operator.stale_head_action(document)
        assert "re-raise the correction against the CURRENT head" in action["description"]

        # 3. a STALE replay is refused server-side first: an action
        #    planned while round 4 was open, replayed against the settled
        #    world, hits the round-status fence (the #349 refusal
        #    extended) — the operator re-decides against the current world
        open_rows = _blocked_round4_rows()
        open_rows["rounds"] = _rounds_through_three() + [
            _round(4, "dispatched", child=R4_CHILD, parent=R3_CHILD, base_head_sha=COMMIT_R3)
        ]
        pre_settle = initial_projection(open_rows, NOW)
        stale_action = next(
            action
            for action in RecoveryActions.plan(pre_settle, "op@example", "approver", at=NOW)
            if action.action == "probe"
        )
        settled = initial_projection(world["rows"], NOW)
        refusal = RecoveryActions.decide(stale_action, settled)
        assert refusal.allowed is False
        assert STALE_ACTION_REFUSAL in refusal.reason

        # 4. the operator executes the runbook action through the guarded
        #    apply (the native /fix admission): round 5 from the EXACT
        #    CURRENT head — the human commit becomes the approved base
        outcome = _guarded_re_raise(world, note_id="1601")
        assert outcome["admitted"] is True
        assert outcome["round_number"] == 5

        # 5. recovery is visible in the view ALONE: round 5 is current and
        #    open, its child is the active execution, its base IS the
        #    preserved human commit, and the stale-head prerequisite is
        #    GONE (the correction slot is held again)
        recovered = operator.observe(
            lineage_view(world["rows"], now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        )
        recovered_current = operator.read_current(recovered)
        assert recovered_current["reference"] == "round 5 of 7319478e"
        assert recovered_current["open"] is True
        assert recovered_current["round_status"] == "admitted"
        assert recovered_current["active_child_run_id"] == outcome["child_run_id"]
        by_number = {entry["round_number"]: entry for entry in recovered["lineage"]["deliveries"]}
        assert by_number[5]["base_head_sha"] == HUMAN_COMMIT  # preserved, never reset
        assert by_number[4]["status"] == "stale"  # history stays immutable
        assert [row["code"] for row in recovered["prerequisites"]] == []

        # 6. the one-outstanding invariant holds on the guarded route: a
        #    second re-raise while round 5 is open is refused
        assert _guarded_re_raise(world, note_id="1602")["admitted"] is False

        # 7. the MR comment the operator leaves behind names the recovered
        #    lineage — one concise block, replay-stable
        first = render_lineage_comment(world["rows"], now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        second = render_lineage_comment(world["rows"], now=NOW, occupancy=OCCUPANCY_UNKNOWN)
        assert first == second
        assert first.splitlines()[0] == "**Forge lineage — round 5 of 7319478e** (root `7319478e`)"


# ---------------------------------------------------------------------------
# The live surface: the reader's lineage sections and the API render
# ---------------------------------------------------------------------------


class TestLiveSurface:
    """The detail route's ``lineage`` block over the READER's sections —
    target, budget, amendments, inbox — from a real sqlite database."""

    @pytest.fixture()
    async def app(self, tmp_path):
        from pydantic import SecretStr

        from forge.adaptive.operator_snapshot import CanonicalSubject
        from forge.config import Settings
        from forge.database import reset_engine
        from forge.main import create_app

        secret = "operator-secret"
        self.secret = secret
        self.subject = CanonicalSubject(
            provider_family="gitlab", connection="gitlab.test", native_id="160"
        )
        reset_engine()
        settings = Settings(
            GITLAB_URL="https://gitlab.test",
            GITLAB_TOKEN=SecretStr("glpat-test"),
            GITLAB_WEBHOOK_SECRET=SecretStr("test-secret-token"),
            DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path}/lineage.db",
            LITELLM_URL="http://litellm:4000",
            REDIS_URL=None,
            FORGE_CAPTURE_DIR=None,
            FORGE_BOT_TOKEN=None,
            FORGE_BOT_USERNAME="forge-bot",
            FORGE_LANE_CONTROL_SECRET=SecretStr(secret),
        )
        application = create_app(settings=settings)
        async with application.router.lifespan_context(application):
            yield application
        reset_engine()

    @pytest.fixture()
    async def client(self, app):
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://forge.test") as http:
            yield http

    async def _seed(self, app, *rows) -> None:
        async with app.state.session_factory() as session:
            session.add_all(rows)
            await session.commit()

    async def test_the_detail_renders_the_lineage_over_the_live_rows(self, app, client):
        from forge.adaptive.admission import ExecutionLease
        from forge.api_operator import operator_subject_scope_token
        from forge.durable.models import (
            BudgetAmendment,
            CollaborationTarget,
            EventInbox,
            ReviewRound,
            RunBudget,
        )
        from forge.durable.models import FlowRun

        now = datetime.now(timezone.utc)

        def _flow(run_id: str, status: str = "proposing") -> FlowRun:
            return FlowRun(
                id=run_id,
                project_id=160,
                provider="gitlab",
                status=status,
                base_sha="b" * 40,
                candidate_shas=[],
                plan_digest="p" * 64,
                evidence={"connection": "gitlab.test"},
                target_id="tgt-1",
                created_at=now - timedelta(hours=3),
                updated_at=now - timedelta(hours=1),
            )

        root_run = _flow(ROOT, "ready_for_human")
        target = CollaborationTarget(
            id="tgt-1",
            provider="gitlab",
            project_ref="160",
            root_run_id=ROOT,
            source_branch=BRANCH,
            target_branch="main",
            mr_iid=4,
            status="active",
            provenance="live",
        )
        rounds = [
            ReviewRound(
                parent_run_id=ROOT,
                child_run_id=R2_CHILD,
                root_run_id=ROOT,
                round_number=2,
                note_id="1497",
                mr_iid=4,
                base_head_sha=CAND_ROOT,
                decision_id="dec-2",
                requested_by="reviewer:op",
                status="completed",
            ),
            ReviewRound(
                parent_run_id=R3_CHILD,
                child_run_id=R4_CHILD,
                root_run_id=ROOT,
                round_number=4,
                note_id="1519",
                mr_iid=4,
                base_head_sha=COMMIT_R3,
                decision_id="dec-4",
                requested_by="reviewer:op",
                status="stale",
                status_reason="the MR head moved off the approved base",
            ),
        ]
        budget = RunBudget(
            run_id=ROOT,
            max_calls=40,
            max_tokens=600_000,
            wallclock_s=3600,
            consumed_calls=12,
            consumed_tokens=240_000,
            closing_partition_policy="closing-partition/1",
            closing_reserved_calls=4,
            closing_reserved_tokens=60_000,
            status="open",
        )
        amendment = BudgetAmendment(
            run_id=ROOT,
            command_id="run:continue_review:160:918",
            axis="calls",
            amount_calls=10,
            reason="operator top-up",
            operator="op@example",
            status="applied",
            limit_before={"calls": 40},
            limit_after={"calls": 50},
        )
        inbox = EventInbox(
            source_event_id="ab" * 32,
            project_id=160,
            event_type="run_command",
            payload={"command": "fix", "project_id": 160, "note_id": "1519"},
        )
        lease = ExecutionLease(
            id="lease-1",
            project_id=160,
            provider="gitlab",
            run_id=ROOT,
            slot=1,
            acquired_at=now - timedelta(minutes=10),
            native_intent_at=now - timedelta(minutes=9),
            native_intent_ref=f"gitlab:pipeline:160@{BRANCH}",
        )
        await self._seed(
            app,
            root_run,
            _flow(R2_CHILD),
            _flow(R4_CHILD),
            target,
            *rounds,
            budget,
            amendment,
            inbox,
            lease,
        )

        token = operator_subject_scope_token(self.secret, [self.subject])
        query = f"subject={self.subject.subject_id()}"
        response = await client.get(
            f"/operator/runs/{ROOT}?{query}", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        document = response.json()
        assert document["schema"] == OPERATOR_VIEW_SCHEMA

        # the lineage block: the human reference, the persisted target,
        # the budget WITH amendments, the command states (the lease is
        # dispatched_unknown → occupancy unknown, never stopped)
        lineage = document["lineage"]
        assert lineage["schema"] == LINEAGE_VIEW_SCHEMA
        assert lineage["lineage"]["reference"] == "round 4 of 7319478e"
        assert lineage["lineage"]["current"]["round_status"] == "stale"
        assert lineage["target"]["source_branch"] == BRANCH
        assert lineage["target"]["mr_iid"] == 4
        assert lineage["budget"]["status"] == "open"
        assert lineage["budget"]["axes"]["calls"]["limit"] == 40
        assert lineage["budget"]["amendments"][0]["axis"] == "calls"
        assert lineage["budget"]["amendments"][0]["amount"] == 10
        assert lineage["occupancy"]["native"] == "unknown"
        states = {entry["command_state"] for entry in lineage["command_states"]}
        assert states == {"received"}  # the inbox row — nothing durably accepted it
        assert document["source_coverage"]["target"] == "present"
        assert document["source_coverage"]["budget"] == "present"
        assert document["source_coverage"]["amendments"] == "present"
        assert document["source_coverage"]["inbox"] == "present"
        # the read-model's lineage fold rides the same render
        ops_lineage = document["ops_limits"]["lineage"]
        assert ops_lineage["reference"] == "round 4 of 7319478e"
        assert ops_lineage["command_state_counts"]["received"] == 1
        assert ops_lineage["projection.stale_or_inconsistent"]["stale_or_inconsistent"] is False

        # the support bundle carries the credential-free lineage slice
        bundle = await client.get(
            f"/operator/runs/{ROOT}/support-bundle?{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert bundle.status_code == 200
        support = bundle.json()["lineage_support"]
        assert support["sections"]["target"]["source_branch"] == BRANCH
        assert "amount" in support["sections"]["budget"]["amendments"][0]
        assert support["export"]["redactions"] >= 0

    async def test_an_unknown_section_selection_is_refused(self, app, client):
        from forge.api_operator import operator_subject_scope_token

        token = operator_subject_scope_token(self.secret, [self.subject])
        response = await client.get(
            f"/operator/runs/{ROOT}?subject={self.subject.subject_id()}&sections=branch",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 400
        assert "branch" in response.json()["detail"]
