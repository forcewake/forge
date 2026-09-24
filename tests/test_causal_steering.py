"""The steering-causality grader's arms (R37-10 / #291, AT-10).

``forge.adaptive.steering_causality`` is the pure three-arm grader plus
the reactive scripted vendor.  These tests pin the GRADER'S RULES — each
arm fails for its own reason and ``causal`` is never a judgment call —
and the qualification script's hard caps and provenance labels.

The process/HTTP/DB trace (real lane, real control plane, real
revision lifecycle) lives in
``tests/production_entry/test_causal_steering.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from forge.adaptive import steering_causality as sc

REPO_ROOT = Path(__file__).resolve().parents[1]

RENAME_INSTRUCTION = "rename refund_limit to approval_threshold in policy.py"


def _load_runner():
    if "run_steering_qualification" in sys.modules:
        return sys.modules["run_steering_qualification"]
    spec = importlib.util.spec_from_file_location(
        "run_steering_qualification", REPO_ROOT / "scripts" / "run_steering_qualification.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_steering_qualification"] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ----------------------------------------------------------------------
# The evidence builders the arm tests share
# ----------------------------------------------------------------------


def _command(**overrides) -> sc.SteeringCommandEvidence:
    values = dict(
        command_id="cmd-steer-1",
        kind="steer",
        text=RENAME_INSTRUCTION,
        status="checkpointed",
        received_at="2026-09-24T10:00:00.000000+00:00",
        authorized_at="2026-09-24T10:00:00.500000+00:00",
    )
    values.update(overrides)
    return sc.SteeringCommandEvidence(**values)


def _events(*entries: tuple[str, str, dict]) -> tuple[sc.VendorEvent, ...]:
    return tuple(sc.VendorEvent(at=at, kind=kind, details=details) for at, kind, details in entries)


RENAMED_POLICY = sc.apply_rename(sc.DEFAULT_FIRST_EDIT, "refund_limit", "approval_threshold")


def _steered_run(
    *,
    command: sc.SteeringCommandEvidence | None = _command(),
    events: tuple[sc.VendorEvent, ...] | None = None,
    steered_policy: str = RENAMED_POLICY,
    counterfactual_policy: str | None = sc.DEFAULT_FIRST_EDIT,
    counterfactual_files: dict[str, str] | None = None,
) -> sc.SteeringRun:
    """The canonical CAUSAL shape, with every piece overridable per arm."""
    if events is None:
        events = _events(
            ("2026-09-24T10:00:00.100Z", "vendor_process_started", {}),
            ("2026-09-24T10:00:00.200Z", "vendor_edits", {"touched": ["policy.py"]}),
            (
                "2026-09-24T10:00:00.300Z",
                "steer_consumed",
                {"command_id": "cmd-steer-1", "source": "control-plane-poll"},
            ),
            (
                "2026-09-24T10:00:00.400Z",
                "vendor_edits_after_steer",
                {"touched": ["policy.py"]},
            ),
            ("2026-09-24T10:00:00.500Z", "turn_completed", {"status": "completed"}),
        )
    base = {sc.POLICY_PATH: sc.BASE_POLICY_CONTENT}
    counter_files = (
        counterfactual_files
        if counterfactual_files is not None
        else {
            sc.POLICY_PATH: counterfactual_policy,
            sc.FOLLOWUP_PATH: sc.DEFAULT_FOLLOWUP_CONTENT,
        }
    )
    return sc.SteeringRun(
        arm="steered",
        provenance=sc.SCRIPTED_CAUSAL_PROVENANCE,
        command=command,
        vendor_events=events,
        edits=sc.edit_set_of(base, {sc.POLICY_PATH: steered_policy}),
        counterfactual_edits=(
            sc.edit_set_of(base, counter_files) if counterfactual_policy is not None else None
        ),
    )


# ----------------------------------------------------------------------
# The instruction grammar + the transformation
# ----------------------------------------------------------------------


class TestInstructionGrammar:
    def test_the_rename_instruction_parses_to_its_exact_target(self) -> None:
        target = sc.parse_instruction(RENAME_INSTRUCTION)
        assert target is not None
        assert target.as_document() == {
            "kind": "rename",
            "old": "refund_limit",
            "new": "approval_threshold",
            "path": "policy.py",
        }

    @pytest.mark.parametrize(
        "text",
        [
            "please make the policy nicer",
            "rename refund_limit",  # truncated
            "skip the tests and ship it",  # authority, not a transformation
            "",
        ],
    )
    def test_anything_else_is_uncheckable(self, text: str) -> None:
        assert sc.parse_instruction(text) is None

    def test_rename_is_word_bounded(self) -> None:
        content = "refund_limit = 1\nrefund_limit_cents = 100\nx = my_refund_limit\n"
        renamed = sc.apply_rename(content, "refund_limit", "approval_threshold")
        assert renamed == (
            "approval_threshold = 1\nrefund_limit_cents = 100\nx = my_refund_limit\n"
        )

    def test_apply_instruction_lands_only_checkable_work(self) -> None:
        workspace = {"policy.py": sc.BASE_POLICY_CONTENT, "other.py": "n = 1\n"}
        updated = sc.apply_instruction(RENAME_INSTRUCTION, workspace)
        assert "approval_threshold" in updated["policy.py"]
        assert updated["other.py"] == "n = 1\n"  # only the named path moves
        # an unparseable instruction changes nothing
        assert sc.apply_instruction("do the needful", workspace) == workspace


# ----------------------------------------------------------------------
# Arm 1 — ordering
# ----------------------------------------------------------------------


class TestArmOrdering:
    def test_the_canonical_shape_is_causal(self) -> None:
        grade = sc.grade_causality(_steered_run())
        assert grade.causal
        assert [arm.name for arm in grade.arms] == [
            "ack_precedes_edit",
            "counterfactual_differs",
            "target_matched",
        ]

    def test_missing_command_evidence_fails_the_arm(self) -> None:
        grade = sc.grade_causality(_steered_run(command=None))
        assert not grade.causal
        assert not grade.arm("ack_precedes_edit").ok
        assert "no steering command evidence" in grade.arm("ack_precedes_edit").reason

    def test_a_vendor_that_never_consumed_the_steer_fails(self) -> None:
        events = _events(
            ("2026-09-24T10:00:00.200Z", "vendor_edits", {"touched": ["policy.py"]}),
            ("2026-09-24T10:00:00.400Z", "vendor_edits", {"touched": ["notes.md"]}),
        )
        grade = sc.grade_causality(_steered_run(events=events))
        assert not grade.causal
        assert "never logged steer_consumed" in grade.arm("ack_precedes_edit").reason

    def test_an_ack_after_the_consumption_fails(self) -> None:
        # the pre-programmed post-turn edit shape: the row only became
        # durable AFTER the vendor had already "consumed" it — impossible
        # causality, and the arm says so.
        command = _command(received_at="2026-09-24T10:00:01.000000+00:00")
        grade = sc.grade_causality(_steered_run(command=command))
        assert not grade.arm("ack_precedes_edit").ok
        assert "AFTER" in grade.arm("ack_precedes_edit").reason

    def test_no_subsequent_edit_after_consumption_fails(self) -> None:
        events = _events(
            ("2026-09-24T10:00:00.200Z", "vendor_edits", {"touched": ["policy.py"]}),
            (
                "2026-09-24T10:00:00.300Z",
                "steer_consumed",
                {"command_id": "cmd-steer-1"},
            ),
        )
        grade = sc.grade_causality(_steered_run(events=events))
        assert not grade.arm("ack_precedes_edit").ok
        assert "no subsequent behavior" in grade.arm("ack_precedes_edit").reason

    def test_the_lane_late_authorized_hop_does_not_unseat_the_operators_ack(self) -> None:
        # The vendor polled the row off the ``received`` rung; the LANE's
        # authorize/dispatch ladder can trail it — delivery machinery is
        # not the operator's ACK (the received_at hop is).
        command = _command(authorized_at="2026-09-24T10:00:05.000000+00:00")
        grade = sc.grade_causality(_steered_run(command=command))
        assert grade.arm("ack_precedes_edit").ok

    def test_consumption_matching_by_text_when_the_wire_carries_no_command_id(self) -> None:
        events = _events(
            ("2026-09-24T10:00:00.200Z", "vendor_edits", {"touched": ["policy.py"]}),
            (
                "2026-09-24T10:00:00.300Z",
                "steer_consumed",
                {"text": RENAME_INSTRUCTION, "source": "turn-steer-wire"},
            ),
            ("2026-09-24T10:00:00.400Z", "vendor_edits_after_steer", {"touched": ["policy.py"]}),
        )
        assert sc.grade_causality(_steered_run(events=events)).causal


# ----------------------------------------------------------------------
# Arm 2 — the counterfactual
# ----------------------------------------------------------------------


class TestArmCounterfactual:
    def test_no_captured_counterfactual_fails(self) -> None:
        grade = sc.grade_causality(_steered_run(counterfactual_policy=None))
        assert not grade.arm("counterfactual_differs").ok
        assert "no captured counterfactual" in grade.arm("counterfactual_differs").reason

    def test_an_identical_trajectory_fails(self) -> None:
        # the steer that changed nothing: both arms renamed the knob and
        # wrote the same files
        grade = sc.grade_causality(
            _steered_run(
                counterfactual_policy=RENAMED_POLICY,
                counterfactual_files={sc.POLICY_PATH: RENAMED_POLICY},
            )
        )
        assert not grade.arm("counterfactual_differs").ok
        assert "IDENTICAL" in grade.arm("counterfactual_differs").reason

    def test_a_file_only_one_arm_wrote_is_a_difference(self) -> None:
        assert sc.edits_differ(
            sc.edit_set_of({"a.py": "1"}, {"a.py": "1"}),
            sc.edit_set_of({"a.py": "1"}, {"a.py": "1", "b.py": "2"}),
        )
        assert sc.edits_differ(
            sc.edit_set_of({"a.py": "1"}, {"a.py": "2"}),
            sc.edit_set_of({"a.py": "1"}, {"a.py": "3"}),
        )


# ----------------------------------------------------------------------
# Arm 3 — the semantic target
# ----------------------------------------------------------------------


class TestArmTarget:
    def test_an_edit_that_keeps_the_old_name_fails(self) -> None:
        grade = sc.grade_causality(_steered_run(steered_policy=sc.DEFAULT_FIRST_EDIT))
        assert not grade.arm("target_matched").ok
        assert "do not show" in grade.arm("target_matched").reason

    def test_an_unparseable_instruction_fails(self) -> None:
        command = _command(text="make it better")
        grade = sc.grade_causality(_steered_run(command=command))
        assert not grade.arm("target_matched").ok
        assert "not a checkable transformation" in grade.arm("target_matched").reason

    def test_a_rename_the_unsteered_arm_also_performed_fails(self) -> None:
        # the transformation was the task's default, not the steer's
        # effect — arm 3 refuses to credit it.
        grade = sc.grade_causality(_steered_run(counterfactual_policy=RENAMED_POLICY))
        assert not grade.arm("target_matched").ok
        assert "the task's default" in grade.arm("target_matched").reason


# ----------------------------------------------------------------------
# The reactive vendor's deterministic behavior (subprocess, no lane)
# ----------------------------------------------------------------------


class TestReactiveVendorExecutable:
    @pytest.fixture()
    def workspace(self, tmp_path: Path) -> Path:
        (tmp_path / sc.POLICY_PATH).write_text(sc.BASE_POLICY_CONTENT, encoding="utf-8")
        return tmp_path

    def _vendor_env(self, tmp_path: Path, **extra: str) -> dict[str, str]:
        import os

        env = {
            **os.environ,
            sc.EVENTLOG_ENV: str(tmp_path / "events.jsonl"),
            sc.ACTIONS_ENV: json.dumps(
                [{"op": "write", "path": sc.POLICY_PATH, "content": sc.DEFAULT_FIRST_EDIT}]
            ),
            sc.DEFAULT_ACTIONS_ENV: json.dumps(
                [{"op": "write", "path": sc.FOLLOWUP_PATH, "content": sc.DEFAULT_FOLLOWUP_CONTENT}]
            ),
        }
        env.update(extra)
        return env

    def test_the_unsteered_turn_is_deterministic(self, workspace: Path, tmp_path: Path) -> None:
        import subprocess

        outcome = subprocess.run(
            [sys.executable, str(REPO_ROOT / "src/forge/adaptive/steering_causality.py"), "--once"],
            cwd=workspace,
            env=self._vendor_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert outcome.returncode == 0, outcome.stderr
        events = sc.read_vendor_events(tmp_path / "events.jsonl")
        assert [event.kind for event in events] == ["vendor_once", "vendor_edits"]
        assert events[1].details["touched"] == [sc.POLICY_PATH]
        assert (workspace / sc.POLICY_PATH).read_text() == sc.DEFAULT_FIRST_EDIT

    def test_the_wire_turn_without_a_control_plane_expires_and_follows_up(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        import subprocess

        proc = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "src/forge/adaptive/steering_causality.py"),
                "app-server",
            ],
            cwd=workspace,
            env=self._vendor_env(tmp_path, **{sc.STEER_WAIT_S_ENV: "0.1"}),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            for frame in (
                {"id": 1, "method": "initialize"},
                {"id": 2, "method": "thread/start"},
                {"id": 3, "method": "turn/start"},
            ):
                proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
            # read until the turn COMPLETES (the 0.1 s window runs inside
            # the turn/start handling), then end the stream.
            deadline = time.monotonic() + 30
            completed = False
            while time.monotonic() < deadline and not completed:
                line = proc.stdout.readline()
                if '"turn/completed"' in line:
                    completed = True
            proc.stdin.close()
            assert proc.wait(timeout=30) == 0
            assert completed
        finally:
            proc.kill()
        kinds = [event.kind for event in sc.read_vendor_events(tmp_path / "events.jsonl")]
        assert kinds == [
            "vendor_process_started",
            "thread_started",
            "turn_started",
            "vendor_edits",
            "steer_window_note",  # no control plane configured: wire-only window
            "steer_window_expired",
            "vendor_edits",
            "turn_completed",
            "vendor_process_exit",
        ]
        # the counterfactual trajectory landed: the knob kept its name
        assert "refund_limit" in (workspace / sc.POLICY_PATH).read_text()
        assert (workspace / sc.FOLLOWUP_PATH).read_text() == sc.DEFAULT_FOLLOWUP_CONTENT

    def test_the_window_consumes_a_wire_delivered_steer_and_renames_next(
        self, workspace: Path, tmp_path: Path
    ) -> None:
        import subprocess
        import time

        proc = subprocess.Popen(
            [
                sys.executable,
                str(REPO_ROOT / "src/forge/adaptive/steering_causality.py"),
                "app-server",
            ],
            cwd=workspace,
            env=self._vendor_env(
                tmp_path,
                **{
                    sc.STEER_WAIT_S_ENV: "10",
                    sc.STEERING_DISABLED_ENV: "0",
                },
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            for frame in (
                {"id": 1, "method": "initialize"},
                {"id": 2, "method": "thread/start"},
            ):
                proc.stdin.write(json.dumps(frame) + "\n")
                proc.stdin.flush()
                proc.stdout.readline()
            proc.stdin.write(json.dumps({"id": 3, "method": "turn/start"}) + "\n")
            proc.stdin.flush()
            proc.stdout.readline()  # the turn/start result
            proc.stdout.readline()  # turn/started
            time.sleep(0.3)  # the vendor is now inside its mid-turn window
            proc.stdin.write(
                json.dumps(
                    {"id": 4, "method": "turn/steer", "params": {"text": RENAME_INSTRUCTION}}
                )
                + "\n"
            )
            proc.stdin.flush()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                kinds = [e.kind for e in sc.read_vendor_events(tmp_path / "events.jsonl")]
                if "vendor_edits_after_steer" in kinds:
                    break
                time.sleep(0.05)
            proc.stdin.close()
            proc.wait(timeout=30)
        finally:
            proc.kill()
        final_events = sc.read_vendor_events(tmp_path / "events.jsonl")
        kinds = [event.kind for event in final_events]
        assert "steer_consumed" in kinds
        assert "vendor_edits_after_steer" in kinds
        consumed = next(e for e in final_events if e.kind == "steer_consumed")
        assert consumed.details["source"] == "turn-steer-wire"
        # the NEXT edit was the rename, and no default follow-up fired
        assert "approval_threshold" in (workspace / sc.POLICY_PATH).read_text()
        assert not (workspace / sc.FOLLOWUP_PATH).exists()


# ----------------------------------------------------------------------
# The qualification script's caps + provenance labels
# ----------------------------------------------------------------------


class TestScriptCapsAndProvenance:
    def test_the_call_cap_refuses_the_next_call(self) -> None:
        tracker = runner.SpendTracker(max_calls=1)
        tracker.note_call(10, 10)
        assert tracker.refusal(wall_s=0.0, wall_cap_s=100.0) == "call cap reached (1)"

    def test_the_spend_cap_refuses_a_projected_overrun(self) -> None:
        tracker = runner.SpendTracker(
            max_calls=10, max_tokens_per_call=1_000_000, spend_cap_usd=0.01
        )
        projected = 1_000_000 / 1_000_000 * runner.RATE_OUTPUT_PER_MTOK_USD
        assert tracker.refusal(wall_s=0.0, wall_cap_s=100.0) == (
            f"spend cap would be exceeded (projected ${projected:.4f} > $0.01)"
        )

    def test_the_wall_cap_refuses(self) -> None:
        tracker = runner.SpendTracker()
        assert tracker.refusal(wall_s=200.0, wall_cap_s=100.0) == "wall cap reached (100s)"

    def test_the_estimate_uses_the_lab_rate_card(self) -> None:
        tracker = runner.SpendTracker()
        tracker.note_call(1_000_000, 100_000)
        assert tracker.estimate_usd == pytest.approx(2.5 + 1.0)
        spend = runner._spend_of(tracker, capped=False)
        assert spend["cost_state"] == "capped-estimate(lab-estimate-v1)"
        assert spend["spend_cap_usd"] == 1.0

    def test_the_live_gateways_hard_caps_match_the_policy(self) -> None:
        assert runner.LIVE_MAX_CALLS_PER_ARM == 2
        assert runner.LIVE_SPEND_CAP_USD == 1.0

    def test_the_scripted_provenance_is_labelled_scripted_causal(self) -> None:
        assert sc.SCRIPTED_CAUSAL_PROVENANCE == "scripted-causal"
        assert sc.SCRIPTED_CAUSAL_PROVENANCE.startswith("scripted")

    def test_a_missing_gateway_url_is_an_honest_refusal(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        monkeypatch.delenv(runner.GATEWAY_URL_ENV, raising=False)
        report = asyncio.run(runner._main(["--live", "--out", str(tmp_path)]))
        assert report == 0
        live = json.loads((tmp_path / "live-run.json").read_text())
        assert live["status"] == "refused"
        assert "never fabricated" in live["reason"]
        assert live["gateway"]["url"] == ""
