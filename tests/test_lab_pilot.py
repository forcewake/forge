"""The lab-pilot runner's machinery (R36-16 / issue #275).

``scripts/run_lab_pilot.py`` executes the lab-recorded operational
pilot over the production-entry seams.  These tests pin the runner's
MACHINERY — not a full pilot run:

- the shipped spec/tasks validate through the kit and carry the
  designed scenario mix, the lab provenance and the unpaid fee note;
- the per-task environment is REAL (a serving control plane over HTTP,
  a durable database file);
- ONE full scripted task records through the tracker with honest
  metrics math (unknown cost stays unknown — never zero);
- a scope violation HALTS the pilot with preserved diagnostics and the
  remaining tasks unrun;
- a blocked task stays in every denominator — never dropped;
- the report rebuilds from the records alone, byte-identically;
- the evidence class is labeled lab-operational everywhere it must be.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import httpx
import pytest

from forge.adaptive import pilot as kit

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "evaluation" / "pilot" / "lab-pilot-v1" / "spec.json"
TASKS_PATH = REPO_ROOT / "evaluation" / "pilot" / "lab-pilot-v1" / "tasks.json"


def _load_runner():
    if "run_lab_pilot" in sys.modules:
        return sys.modules["run_lab_pilot"]
    spec = importlib.util.spec_from_file_location(
        "run_lab_pilot", REPO_ROOT / "scripts" / "run_lab_pilot.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_lab_pilot"] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ----------------------------------------------------------------------
# The shipped plan
# ----------------------------------------------------------------------


def test_shipped_lab_pilot_plan_validates_and_covers_the_designed_mix() -> None:
    plan, drivers = runner.load_lab_plan(SPEC_PATH, TASKS_PATH)
    plan.validate()
    assert len(plan.tasks) == 12
    assert all(task.task_id in drivers for task in plan.tasks)

    scenarios = Counter(task.scenario for task in plan.tasks)
    # The designed mix: ordinary x3, ambiguity x2, neighbor x2,
    # intervention x2, infra-failure+retry x2, cross-runner resume x1.
    assert (
        scenarios["test_repair"] + scenarios["cold_reinstall"] + scenarios["adaptive_control"] == 3
    )
    assert scenarios["ambiguous_task"] + scenarios["missing_requirement"] == 2
    assert scenarios["neighbor_evidence"] == 2
    assert scenarios["human_intervention"] == 2
    assert scenarios["runner_loss"] + scenarios["resource_revocation"] == 3  # infra x2 + resume x1
    assert set(kit.SCENARIO_TAGS) <= set(scenarios)  # every kit tag exercised

    # The groups stage in ladder order (the kit enforces; assert the shape).
    stage_of = {task.group: task.stage for task in plan.tasks}
    ordered = [stage_of[group] for group in plan.spec.review_groups]
    assert ordered == [stage for stage in kit.STAGES if stage in set(ordered)]

    spec = plan.spec
    assert "lab-operational-pilot" in spec.provenance_labels
    assert "UNPAID" in spec.fee_note  # the fee-as-demand-test is honestly unexecuted
    assert spec.baseline.window_end <= spec.window_start  # frozen BEFORE kickoff
    assert len(spec.criteria) == kit.CRITERIA_REQUIRED
    assert all(c.metric in kit.CRITERION_METRICS for c in spec.criteria)


# ----------------------------------------------------------------------
# The real per-task environment
# ----------------------------------------------------------------------


async def test_task_environment_serves_the_control_plane_over_real_http(tmp_path: Path) -> None:
    async with runner.LabEnvironment(tmp_path, "lab-env-probe") as env:
        assert env.db_path.is_file()  # real durable state
        assert env.control_url.startswith("http://127.0.0.1:")
        # The lane-control surface is SERVING and refuses the unauthenticated
        # (the lane's work-scoped token is the only way in).
        response = httpx.get(
            env.control.url("/lane/controls"),
            params={"work_id": "lab-env-probe"},
            timeout=10.0,
        )
        assert response.status_code == 401
        assert "bearer" in response.json()["detail"].lower()


# ----------------------------------------------------------------------
# One full scripted task, recorded through the tracker
# ----------------------------------------------------------------------


async def test_one_scripted_task_records_and_folds_with_honest_math(tmp_path: Path) -> None:
    plan, drivers = runner.load_lab_plan(SPEC_PATH, TASKS_PATH)
    task = next(task for task in plan.tasks if task.task_id == "t-01-repair")
    record, evidence = await runner.drive_one_task(task, drivers[task.task_id], tmp_path)

    # The task record: accepted, BOTH attempts kept, the forced plan
    # correction recorded, latency measured.
    assert record.accepted is True
    assert record.blocked == ""
    assert len(record.attempts) == 2
    assert record.attempts[0].accepted is False  # the broken attempt stays
    assert record.attempts[-1].accepted is True
    assert record.plan_corrections == 1
    assert record.completion_latency_minutes is not None
    assert record.setup_minutes >= 0 and record.review_minutes >= 0

    # The evidence: every seam check ok, the acceptance decision went
    # PENDING -> DECIDED, the usage ledger is #276-shaped with the cost
    # UNKNOWN and labeled as such.
    assert all(entry["ok"] for entry in evidence["seam_checks"])
    states = [d["state"] for d in evidence["operator_decisions"] if d["point"] == "acceptance"]
    assert states == ["PENDING", "DECIDED"]
    for row in evidence["usage_ledger"]:
        assert row["spend_usd"] is None
        assert row["cost_state"] == "unknown"
        assert "never zero" in row["cost_note"]
    assert evidence["evidence_class"] == runner.EVIDENCE_CLASS
    assert (tmp_path / task.task_id / "task-evidence.json").is_file()
    assert (tmp_path / task.task_id / "candidate.diff").is_file()

    # The tracker fold over this one task: the honest metrics math.
    tracker = kit.PilotTracker(plan.spec, kit.record_onboarding(plan.spec))
    tracker.record_task(record)
    metrics = tracker.metrics()
    assert metrics.tasks_total == 1
    assert metrics.tasks_accepted == 1
    assert metrics.autonomy_rate == 1.0
    assert metrics.intervention_rate == 0.0
    assert metrics.total_cost_usd is None  # unknown, never zero
    assert metrics.cost_per_accepted_usd is None
    assert any("cost unknown" in note for note in metrics.notes)
    assert metrics.cycle_time_vs_baseline is not None
    assert metrics.cycle_time_vs_baseline < 1.0


# ----------------------------------------------------------------------
# Stop conditions: halt with preserved diagnostics
# ----------------------------------------------------------------------


async def test_scope_violation_halts_the_pilot_and_preserves_diagnostics(
    tmp_path: Path,
) -> None:
    plan, drivers = runner.load_lab_plan(SPEC_PATH, TASKS_PATH)

    async def violating_driver(setup: object) -> runner.TaskDrive:
        return runner.TaskDrive(
            accepted=True,
            attempts=[runner.AttemptLog(attempt_id="", wall_seconds=0.1)],
            touched_repos=["outside/the-boundary"],  # the stop-rule trigger
            completion_latency_minutes=0.1,
        )

    registry = dict(runner.DRIVERS)
    registry["cold_reinstall"] = violating_driver
    tracker = await runner.run_pilot(
        plan, drivers, tmp_path, registry=registry, only=("t-02-cold",)
    )

    assert tracker.violations and tracker.violations[0].kind == kit.VIOLATION_SCOPE
    assert tracker.diagnostics is not None
    assert "stop rule" in tracker.diagnostics.stop_reason
    with pytest.raises(kit.PilotError):
        tracker.record_task(  # a preserved pilot NEVER records again
            kit.TaskRecord(task_id="after-stop", started_at="2026-09-23T00:00:00+00:00")
        )
    run_state = json.loads((tmp_path / "records" / "run-state.json").read_text(encoding="utf-8"))
    assert "stop rule" in run_state["stop_reason"]
    report = json.loads((tmp_path / runner.REPORT_FILE).read_text(encoding="utf-8"))
    assert report["stop"]["reason"].startswith("stop rule")
    assert report["stop"]["retried"] is False
    assert report["stop"]["diagnostics_pointer"] == tracker.diagnostics.pointer


# ----------------------------------------------------------------------
# Blocked tasks: the denominator keeps them
# ----------------------------------------------------------------------


async def test_a_blocked_task_stays_in_every_denominator(tmp_path: Path) -> None:
    plan, drivers = runner.load_lab_plan(SPEC_PATH, TASKS_PATH)

    async def blocked_driver(setup: object) -> runner.TaskDrive:
        raise runner.BlockScenario("seam gap: the scenario cannot run on this seam")

    registry = dict(runner.DRIVERS)
    registry["cold_reinstall"] = blocked_driver
    tracker = await runner.run_pilot(
        plan, drivers, tmp_path, registry=registry, only=("t-02-cold",)
    )

    (record,) = tracker.records
    assert record.accepted is False
    assert record.blocked.startswith("blocked: seam gap")
    metrics = tracker.metrics()
    assert metrics.tasks_total == 1  # in the denominator, never dropped
    assert metrics.tasks_accepted == 0
    assert metrics.autonomy_rate == 0.0
    assert metrics.intervention_rate == 0.0
    report = json.loads((tmp_path / runner.REPORT_FILE).read_text(encoding="utf-8"))
    entry = report["tasks"]["t-02-cold"]
    assert entry["blocked"].startswith("blocked: seam gap")
    assert entry["unaccepted"] is True
    evidence = json.loads(
        (tmp_path / "records" / "t-02-cold" / "task-evidence.json").read_text(encoding="utf-8")
    )
    assert evidence["blocked"].startswith("blocked: seam gap")


# ----------------------------------------------------------------------
# Report determinism: rebuild from the records alone
# ----------------------------------------------------------------------


def _synthetic_tracker(plan: kit.PilotPlan) -> kit.PilotTracker:
    tracker = kit.PilotTracker(
        plan.spec,
        kit.record_onboarding(plan.spec, recorded_at="2026-09-23T09:00:00+00:00"),
    )
    base = {"started_at": "2026-09-23T10:00:00+00:00", "touched_repos": ("lab/checkout",)}
    tracker.record_task(
        kit.TaskRecord(
            task_id="t-01-repair",
            accepted=True,
            attempts=(kit.AttemptUsage("a1", accepted=False),),
            completion_latency_minutes=1.0,
            review_minutes=2.0,
            **base,
        )
    )
    tracker.record_task(
        kit.TaskRecord(
            task_id="t-02-cold",
            accepted=False,
            manual_rescues=1,
            rescue_notes=("operator finished the collection by hand",),
            attempts=(kit.AttemptUsage("a1", accepted=False),),
            interventions=(kit.InterventionEvent(kind="steer", note="tighten"),),
            **base,
        )
    )
    tracker.record_task(
        kit.TaskRecord(task_id="t-03-adaptive", blocked="blocked: synthetic", **base)
    )
    return tracker


def test_report_rebuilds_identically_from_the_records(tmp_path: Path) -> None:
    plan, _drivers = runner.load_lab_plan(SPEC_PATH, TASKS_PATH)
    tracker = _synthetic_tracker(plan)
    runner.finalize_pilot(plan, tracker, tmp_path)
    first = (tmp_path / runner.REPORT_FILE).read_bytes()

    rebuilt = runner.rebuild_report(tmp_path, spec_path=SPEC_PATH, tasks_path=TASKS_PATH)
    assert (tmp_path / runner.REPORT_FILE).read_bytes() == first
    assert rebuilt.digest == kit.PilotReport(document=json.loads(first)).digest

    # The rebuilt report keeps the unaccepted task, the rescue and the
    # blocked task visible (nothing was silently dropped on replay).
    report = json.loads(first)
    assert report["tasks"]["t-02-cold"]["unaccepted"] is True
    assert report["tasks"]["t-02-cold"]["rescue_notes"]
    assert report["tasks"]["t-03-adaptive"]["blocked"] == "blocked: synthetic"


# ----------------------------------------------------------------------
# Provenance and the evidence class
# ----------------------------------------------------------------------


def test_provenance_and_evidence_class_label_every_artifact(tmp_path: Path) -> None:
    plan, _drivers = runner.load_lab_plan(SPEC_PATH, TASKS_PATH)
    tracker = _synthetic_tracker(plan)
    runner.finalize_pilot(plan, tracker, tmp_path)

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert "lab-operational-pilot" in spec["provenance_labels"]
    assert "no-paid-models" in spec["provenance_labels"]

    report = json.loads((tmp_path / runner.REPORT_FILE).read_text(encoding="utf-8"))
    assert report["provenance_labels"] == spec["provenance_labels"]

    bounding = json.loads((tmp_path / runner.BOUNDING_FILE).read_text(encoding="utf-8"))
    assert bounding["evidence_class"] == runner.EVIDENCE_CLASS
    assert bounding["provenance"] == "lab-operational-pilot"
    assert bounding["live_blockers_cross_ref"]["issue"] == 268
    assert any("scripted vendor" in item for item in bounding["what_did_not_run"])
    assert any("partner" in item for item in bounding["deviations_from_the_partner_pilot_shape"])
