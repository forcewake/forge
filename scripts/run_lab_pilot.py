#!/usr/bin/env python3
"""R36-16 (issue #275) — the lab-recorded operational pilot runner.

The pilot KIT (``forge.adaptive.pilot``) froze the contract, the task
range and the stop rules.  What was missing is an EXECUTED pilot.  The
lab GitLab CE qualification (#268) honestly REFUSED its live flow — the
deployed control plane (0.28.0) is not the pinned wheel (v0.35.0), no
budget caps exist, and the GitLab dispatch seam carries no lane-resume
contract — so a paid live pilot against the lab would qualify nothing.

The honest executable pilot is this LAB-RECORDED OPERATIONAL pilot: it
drives REAL task execution through the offline production-entry-grade
seams (real git checkouts, real lane subprocesses on the real codex
app-server wire with the controlled scripted vendor, the SHIPPED
collector subprocess, real HTTP control plane, real durable sqlite
state, the real GitHubRunService against the fake native server's
recorded dispatch ledger) while the pilot machinery records everything a
partner pilot would record — with the report explicitly bounding the
evidence class to ``lab-operational``.

Seam reuse is BY IMPORT: the drivers come from ``tests/production_entry``
(``conftest`` + ``test_production_entry`` helpers — ``make_checkout``,
``run_lane``, ``run_collector``, ``upload_wip_checkpoint``,
``start_control_plane``) exactly as the pytest traces drive them.  The
pytest-BOUND pieces (the ``native``/``pe_db`` fixtures) are re-spawned
here by minimal drivers (``_spawn_native``, ``LabEnvironment``) using
the same module-level constants and process discipline.

No paid model runs anywhere: token counts are real wire observations,
every cost is recorded ``unknown`` in the #276 ledger shape — never
zero.  A design-partner pilot remains the next step.

Usage::

    uv run python scripts/run_lab_pilot.py --out evaluation/pilot/lab-pilot-v1/
    uv run python scripts/run_lab_pilot.py --rebuild-from evaluation/pilot/lab-pilot-v1/
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # seam reuse by import needs the repo root
    sys.path.insert(0, str(REPO_ROOT))

from forge.adaptive import pilot as kit  # noqa: E402
from forge.adaptive.checkpoint_channel import work_scoped_token  # noqa: E402
from forge.adaptive.discovery import (  # noqa: E402
    DiscoveryRun,
    plan_digest,
    structured_plan,
    validate_plan_citations,
)
from forge.durable import FlowRun  # noqa: E402

# The production-entry seams, reused BY IMPORT (Q35-09's layer).
from tests.production_entry import conftest as pe  # noqa: E402
from tests.production_entry import test_production_entry as pe_helpers  # noqa: E402

EVIDENCE_CLASS = "lab-operational"
BOUNDING_SCHEMA = "forge.lab-pilot.bounding/1"
RUN_STATE_SCHEMA = "forge.lab-pilot.run-state/1"
LAB_OPERATOR = "human:lab-operator"
SPEC_FILE = "spec.json"
TASKS_FILE = "tasks.json"
REPORT_FILE = "report.json"
BOUNDING_FILE = "lab-bounding.json"
RECORDS_DIRNAME = "records"

#: The #268 refusal reasons this pilot's existence is bounded by — the
#: live blockers a real partner pilot on the lab GitLab would hit.
LIVE_BLOCKERS = {
    "issue": 268,
    "refused_stages": [
        "flow (paid): controlplane.version_matches_profile — the deployed control plane reports 0.28.0; the profile pins the promoted wheel v0.35.0 (a paid flow against another build qualifies nothing)",
        "flow (paid): budgets.caps_present — no FORGE_BUDGET_PROFILES and no --max-budget-json (an uncapped paid run is not qualification)",
        "cross-runner resume drill: capability.lane_resume_dispatch — the GitLab dispatch seam carries no FORGE_LANE_RESUME/lane-control credentials (GitHub-only, R32-04; profile gitlab-ce-v1 §8)",
    ],
}

#: What ran for real, published verbatim in the bounding document.
REAL_SEAMS = [
    "real git checkouts at a frozen base (the lane's starting shape), reused from tests/production_entry",
    "the REAL lane subprocess (python -m forge.lane_driver --driver codex) with the controlled scripted vendor speaking the real codex app-server JSON-RPC wire",
    "the SHIPPED collector subprocess (python -m forge.harness_entry --collect-candidate)",
    "the REAL capture_wip/restore_wip(promote='generation') checkpoint machinery uploaded/downloaded over real HTTP through the lane-control + checkpoint-channel routers on uvicorn",
    "real durable sqlite state (a restarted worker is a fresh engine/session factory over the same rows)",
    "the REAL GitHubRunService + GitHubClient over real HTTP against the fake native server's recorded dispatch ledger",
    "the REAL durable control surface (OperatorControlService over PostgresMailbox: steer/pause/resume/answer command rows)",
]

#: What did NOT run — the honesty section beside the seams.
NOT_REAL = [
    "no paid model call anywhere: the vendor is the controlled scripted executable (real wire, scripted edits); token counts are wire observations, spend is unknown — never zero",
    "no design-partner customer: the lab operator (the maintainer) is the only human; no customer decided usefulness",
    "no partner engineering review: acceptance is the operator's independent mechanical verification over seam artifacts, never PR creation and never a model summary",
    "no live provider: the native surface is the fake native server (its state survives worker death by construction, but it is not GitHub or GitLab)",
    "mid-turn steer DELIVERY is not demonstrable with the scripted vendor (its turn completes faster than any poll cadence): the durable steer row, the real drain over HTTP and the journal's honest error outcome are the recorded evidence — a landed steer needs a real vendor",
    "no 4-8 week window, no J-curve, no adoption learning: the lab window is its execution day",
]


class PilotRunnerError(RuntimeError):
    """The pilot runner hit a condition it refuses to paper over."""


class BlockScenario(Exception):
    """A scenario that genuinely cannot run (a seam gap) — the task is
    recorded ``blocked:<reason>`` and stays in every denominator."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_lab_plan(
    spec_path: Path | str, tasks_path: Path | str
) -> tuple[kit.PilotPlan, dict[str, dict[str, Any]]]:
    """Load + validate the spec and task set through the kit, and return
    the per-task driver parameters beside the frozen plan."""
    spec = kit.PilotSpec.load(Path(spec_path))
    document = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
    drivers = document.get("drivers") or {}
    if not isinstance(drivers, Mapping):
        raise PilotRunnerError("tasks document: 'drivers' must be an object")
    plan = kit.PilotPlan(
        plan_id=str(document.get("plan_id") or ""),
        spec=spec,
        tasks=tuple(kit.PilotTask.from_document(entry) for entry in document.get("tasks") or ()),
    )
    plan.validate()
    missing = [task.task_id for task in plan.tasks if task.task_id not in drivers]
    if missing:
        raise PilotRunnerError(f"tasks without a driver scenario: {missing}")
    return plan, {str(key): dict(value) for key, value in drivers.items()}


def default_plan_dir() -> Path:
    return REPO_ROOT / "evaluation" / "pilot" / "lab-pilot-v1"


# ---------------------------------------------------------------------------
# The per-task real environment (the pytest fixtures, re-spawned)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _env_scope(**overrides: str) -> Iterator[dict[str, str]]:
    """Set env vars for the duration (the runner-side monkeypatch)."""
    saved: dict[str, str] = {}
    try:
        for key, value in overrides.items():
            if key in os.environ:
                saved[key] = os.environ[key]
            os.environ[key] = value
        yield overrides
    finally:
        for key in overrides:
            if key in saved:
                os.environ[key] = saved[key]
            else:
                os.environ.pop(key, None)


class LabEnvironment:
    """One task's real infrastructure: a durable sqlite control DB, the
    lane-control + checkpoint-channel routers over real HTTP (uvicorn on
    a loopback port), and a fresh session factory.

    The pytest ``pe_db``/control-plane fixtures re-spawned for script
    use: same construction, same cleanup discipline.
    """

    def __init__(self, root: Path, work_id: str, *, with_durable_runs: bool = False) -> None:
        self.root = root
        self.work_id = work_id
        root.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "control.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.store_dir = root / "control-store"
        self._with_durable_runs = with_durable_runs
        self.engine = None
        self.factory = None
        self.control: pe.ControlPlane | None = None

    async def __aenter__(self) -> LabEnvironment:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        self.engine = create_async_engine(self.db_url)
        await self._create_schema(self.engine)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.control = await pe.start_control_plane(self.db_url)
        return self

    async def _create_schema(self, engine) -> None:
        if self._with_durable_runs:
            # The GitHubRunService traces need the durable run ledger too
            # (same import set as the PE database fixture).
            import forge.adaptive.mailbox_db  # noqa: F401 — control_commands
            import forge.durable.models  # noqa: F401 — FlowRun and friends
            from forge import api_checkpoint_channel  # noqa: F401 — checkpoint_metadata
            from forge.models.base import Base

            assert forge.adaptive.mailbox_db and forge.durable.models
            assert api_checkpoint_channel and Base
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        await pe_helpers.create_control_schema(engine)

    async def __aexit__(self, *_exc: object) -> None:
        if self.control is not None:
            self.control.stop()
        if self.engine is not None:
            await self.engine.dispose()

    @property
    def control_url(self) -> str:
        if self.control is None:
            raise PilotRunnerError("environment not entered")
        return self.control.base_url

    def lane_env_scope(self, **extra: str) -> dict[str, str]:
        """The env vars the lane/checkpoint seams read (PE's monkeypatch)."""
        values = dict(
            FORGE_CHECKPOINT_STORE_DIR=str(self.store_dir),
            FORGE_LANE_CONTROL_SECRET=pe.PE_LANE_SECRET,
            FORGE_LANE_LEGACY_TOKEN_DEADLINE=pe.PE_LEGACY_DEADLINE,
        )
        values.update(extra)
        return values

    def token(self) -> str:
        return work_scoped_token(pe.PE_LANE_SECRET, self.work_id)

    def durable_control(self):
        """The REAL durable operator control service over this DB."""
        from forge.adaptive.checkpoint_repository import resolve_repository
        from forge.adaptive.mailbox_db import PostgresMailbox
        from forge.adaptive.wiring import OperatorControlService

        assert self.factory is not None
        return OperatorControlService(
            mailbox=PostgresMailbox(self.factory),
            checkpoint_repository=resolve_repository(session_factory=self.factory),
        )

    async def control_rows(self, *, kind: str) -> list[dict[str, Any]]:
        """The durable control_commands rows of one kind (evidence reads)."""
        from sqlalchemy import select

        from forge.adaptive.mailbox_db import ControlCommandRow

        assert self.factory is not None
        async with self.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ControlCommandRow).where(ControlCommandRow.kind == kind)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "command_id": row.id,
                    "work_id": row.work_id,
                    "kind": row.kind,
                    "status": row.status,
                    "actor_ref": row.actor_ref,
                    "payload": dict(row.payload or {}),
                }
                for row in rows
            ]


def _spawn_native(root: Path) -> pe.FakeNative:
    """The fake native server as a REAL separate process — the pytest
    ``native`` fixture's spawn, extracted verbatim for script use."""
    ready = root / "native-ready.json"
    process = subprocess.Popen(
        [
            sys.executable,
            str(pe.FAKE_NATIVE_SERVER),
            "--ready-file",
            str(ready),
            "--repo",
            pe.PE_REPO,
            "--base-branch",
            pe.PE_BASE_BRANCH,
            "--base-sha",
            pe.PE_BASE_SHA,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20.0
    while not ready.is_file():
        if process.poll() is not None:
            raise PilotRunnerError("the fake native server died at startup")
        if time.monotonic() > deadline:
            process.kill()
            raise PilotRunnerError("the fake native server never became ready")
        time.sleep(0.02)
    return pe.FakeNative(process, int(json.loads(ready.read_text())["port"]))


# ---------------------------------------------------------------------------
# Evidence plumbing
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minutes(since: float) -> float:
    return round((time.monotonic() - since) / 60.0, 4)


@dataclass
class AttemptLog:
    attempt_id: str
    wall_seconds: float
    note: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    tool_events: int | None = None


@dataclass
class TaskDrive:
    """Everything one driven task produced, before the tracker fold."""

    accepted: bool
    seam_checks: list[dict[str, Any]] = field(default_factory=list)
    attempts: list[AttemptLog] = field(default_factory=list)
    interventions: list[kit.InterventionEvent] = field(default_factory=list)
    operator_decisions: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    touched_repos: list[str] = field(default_factory=lambda: ["lab/checkout"])
    setup_minutes: float = 0.0
    review_minutes: float = 0.0
    completion_latency_minutes: float | None = None
    plan_corrections: int = 0
    plan_correction_notes: list[str] = field(default_factory=list)
    artifacts: list[tuple[str, Path]] = field(default_factory=list)
    #: The review leg's independent RECOUNT inputs (the base checkout and
    #: the recorded candidate diff): the reviewer re-verifies by applying
    #: the diff onto a fresh clone — measured time, never a dressed zero.
    recount: tuple[Path, Path] | None = None


def check(name: str, ok: bool, expected: str, observed: str) -> dict[str, Any]:
    """One seam check — the AT-0x seam outcome as recorded evidence."""
    return {
        "name": name,
        "ok": bool(ok),
        "expected": expected,
        "observed": observed,
    }


def _usage_row(attempt: AttemptLog) -> dict[str, Any]:
    return kit.usage_ledger_document(
        attempt.attempt_id,
        source="lab-lane/vendor-wire",
        input_tokens=attempt.tokens_in,
        output_tokens=attempt.tokens_out,
        model_time_s=round(attempt.wall_seconds, 3),
        tool_call_count=attempt.tool_events,
    )


def _read_lane_meta(checkout: Path) -> dict[str, Any]:
    meta_path = checkout / ".forge" / "candidate.meta.json"
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except ValueError:
        return {}


def _read_usage(checkout: Path) -> dict[str, Any]:
    usage_path = checkout / ".forge" / "usage.json"
    if not usage_path.is_file():
        return {}
    try:
        parsed = json.loads(usage_path.read_text())
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tokens_from_usage(usage: Mapping[str, Any]) -> tuple[int | None, int | None]:
    if not isinstance(usage, Mapping):
        return None, None
    tokens = usage.get("tokens") if isinstance(usage.get("tokens"), Mapping) else usage
    if not isinstance(tokens, Mapping):
        return None, None
    value_in = tokens.get("input_tokens", tokens.get("input"))
    value_out = tokens.get("output_tokens", tokens.get("output"))
    return (
        int(value_in) if isinstance(value_in, int) else None,
        int(value_out) if isinstance(value_out, int) else None,
    )


def _vendor_event_count(eventlog: Path) -> int:
    if not eventlog.is_file():
        return 0
    return sum(1 for line in eventlog.read_text().splitlines() if line.strip())


def _collect(checkout: Path, work_id: str, attempt_base: str) -> dict[str, Any]:
    """Run the SHIPPED collector and return its reported document."""
    outcome = pe_helpers.run_collector(checkout, work_id=work_id, attempt_base=attempt_base)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"the shipped collector failed: {outcome.stderr[-2000:]}")
    return json.loads(outcome.stdout)


def _diff_bytes(reported: Mapping[str, Any], checkout: Path) -> bytes:
    return Path(reported["diff_path"]).read_bytes()


def _content_in_diff(diff_bytes: bytes, content: str, base_content: str = "") -> bool:
    """Whether every line *content* ADDS over *base_content* rides the
    diff as an added line (unified diffs prefix each added line with
    '+'; lines the base already carried stay context lines)."""
    base_lines = set(base_content.splitlines())
    required = [line for line in content.splitlines() if line.strip() and line not in base_lines]
    if not required:
        return not content.strip() or True  # a pure deletion/no-op edit rides nothing
    text = diff_bytes.decode("utf-8", errors="replace")
    return all(f"+{line}" in text for line in required)


def _base_content(checkout: Path, relative: str) -> str:
    """The target file's bytes at the frozen base, read BEFORE the lane runs."""
    target = checkout / relative
    return target.read_text(encoding="utf-8") if target.is_file() else ""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return pe_helpers._git(cwd, *args)


def _apply_check(base_checkout: Path, diff_path: Path) -> tuple[bool, str]:
    """The independent 'diff applies cleanly' check in a fresh clone."""
    with tempfile.TemporaryDirectory(prefix="lab-pilot-apply-") as tmp:
        clone = Path(tmp) / "clone"
        result = subprocess.run(
            ["git", "clone", "-q", "--no-local", str(base_checkout), str(clone)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return False, f"clone failed: {result.stderr.strip()}"
        applied = subprocess.run(
            ["git", "apply", "--check", str(diff_path)],
            cwd=clone,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return applied.returncode == 0, (applied.stderr.strip() or "applies cleanly")


def _compile_check(base_checkout: Path, diff_path: Path, target: str) -> tuple[bool, str]:
    """The independent 'candidate compiles' check — apply the diff in a
    fresh clone, then py_compile the target (never the agent's word)."""
    with tempfile.TemporaryDirectory(prefix="lab-pilot-compile-") as tmp:
        clone = Path(tmp) / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--no-local", str(base_checkout), str(clone)],
            capture_output=True,
            timeout=120,
            check=True,
        )
        applied = subprocess.run(
            ["git", "apply", str(diff_path)],
            cwd=clone,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if applied.returncode != 0:
            return False, f"apply failed: {applied.stderr.strip()}"
        target_path = clone / target
        if not target_path.is_file():
            return False, f"{target} absent after applying the candidate"
        compiled = subprocess.run(
            [sys.executable, "-m", "py_compile", str(target_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return compiled.returncode == 0, (compiled.stderr.strip() or "compiles")


def _decision_pending(decisions: list[dict[str, Any]], point: str, question: str) -> None:
    decisions.append({"point": point, "state": "PENDING", "question": question, "at": _now_iso()})


def _decision_made(
    decisions: list[dict[str, Any]], point: str, decision: str, detail: str = ""
) -> None:
    decisions.append(
        {
            "point": point,
            "state": "DECIDED",
            "decision": decision,
            "detail": detail,
            "decided_by": LAB_OPERATOR,
            "at": _now_iso(),
        }
    )


# ---------------------------------------------------------------------------
# The scenario drivers (all real seams; scripted vendor on the real wire)
# ---------------------------------------------------------------------------


@dataclass
class TaskSetup:
    """What a driver receives: the task, its scenario params, the live
    environment and the records directory for its evidence."""

    task: kit.PilotTask
    params: Mapping[str, Any]
    env: LabEnvironment
    records_dir: Path
    work_id: str


def _fresh_lane(
    env: LabEnvironment,
    checkout: Path,
    *,
    actions: list[dict[str, Any]],
    eventlog: Path,
    resume: str = "",
    poll_seconds: str = "0.05",
) -> subprocess.CompletedProcess[str]:
    """One REAL lane subprocess.  The steering channel's poll cadence is
    tightened through its env knob (``FORGE_LANE_CONTROL_POLL_SECONDS``)
    so a pre-recorded command drains within the scripted vendor's fast
    turn — the default 2 s cadence would miss it."""
    with _env_scope(FORGE_LANE_CONTROL_POLL_SECONDS=poll_seconds):
        return pe_helpers.run_lane(
            checkout,
            work_id=env.work_id,
            resume=resume,
            control_url=env.control_url,
            token=env.token(),
            attempt_base=pe_helpers._git(checkout, "rev-parse", "HEAD").stdout.strip(),
            actions=actions,
            eventlog=eventlog,
            timeout=240,
        )


def _lane_attempt(
    checkout: Path,
    env: LabEnvironment,
    *,
    actions: list[dict[str, Any]],
    eventlog: Path,
    resume: str = "",
    poll_seconds: str = "0.05",
) -> tuple[AttemptLog, subprocess.CompletedProcess[str]]:
    started = time.monotonic()
    outcome = _fresh_lane(
        env, checkout, actions=actions, eventlog=eventlog, resume=resume, poll_seconds=poll_seconds
    )
    log = AttemptLog(attempt_id="", wall_seconds=time.monotonic() - started)
    usage = _read_usage(checkout)
    tokens_in, tokens_out = _tokens_from_usage(usage)
    log.tokens_in, log.tokens_out = tokens_in, tokens_out
    log.tool_events = _vendor_event_count(eventlog)
    return log, outcome


def _fresh_lane_with_poll(
    checkout: Path,
    env: LabEnvironment,
    *,
    actions: list[dict[str, Any]],
    eventlog: Path,
    poll_seconds: str,
) -> tuple[AttemptLog, subprocess.CompletedProcess[str]]:
    started = time.monotonic()
    outcome = _fresh_lane(
        env, checkout, actions=actions, eventlog=eventlog, poll_seconds=poll_seconds
    )
    log = AttemptLog(attempt_id="", wall_seconds=time.monotonic() - started)
    log.tokens_in, log.tokens_out = _tokens_from_usage(_read_usage(checkout))
    log.tool_events = _vendor_event_count(eventlog)
    return log, outcome


def _name_attempts(drive: TaskDrive, prefix: str) -> None:
    for index, attempt in enumerate(drive.attempts, start=1):
        attempt.attempt_id = f"{prefix}/a{index}"


async def drive_test_repair(setup: TaskSetup) -> TaskDrive:
    """t-01: the scripted vendor's first edit BREAKS the independent
    check for real (a syntax error), forcing a recorded plan correction;
    the second attempt repairs it."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    checkout, base = pe_helpers.make_checkout(root, "workspace")
    eventlog = root / "vendor-events.jsonl"
    target = str(params["target_file"])
    brief = str(params["brief"])
    base_target = _base_content(checkout, target)

    # Attempt 1 — the broken edit (the scenario's forced correction).
    attempt, outcome = _lane_attempt(
        checkout,
        setup.env,
        actions=[{"op": "write", "path": target, "content": str(params["broken_edit"])}],
        eventlog=eventlog,
    )
    attempt.note = "first edit failed the independent compile check"
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"lane failed unexpectedly: {outcome.stderr[-2000:]}")
    reported = _collect(checkout, setup.work_id, base)
    diff_path = Path(reported["diff_path"])
    ok, detail = _compile_check(checkout, diff_path, target)
    drive.seam_checks.append(
        check(
            "first-attempt-fails-independent-check",
            not ok,
            "the broken first edit FAILS py_compile (the forced correction is real)",
            detail,
        )
    )
    drive.plan_corrections = 1
    drive.plan_correction_notes = [
        "the scripted vendor's first edit left a syntax error in "
        f"{target}; the operator corrected the plan and re-ran (the failed attempt stays)"
    ]

    # Attempt 2 — the repair.
    attempt2, outcome2 = _lane_attempt(
        checkout,
        setup.env,
        actions=[{"op": "write", "path": target, "content": str(params["repaired_edit"])}],
        eventlog=eventlog,
    )
    attempt2.note = "repaired edit"
    drive.attempts.append(attempt2)
    if outcome2.returncode != 0:
        raise PilotRunnerError(f"repair lane failed: {outcome2.stderr[-2000:]}")
    reported2 = _collect(checkout, setup.work_id, base)
    diff_path2 = Path(reported2["diff_path"])
    diff_bytes = _diff_bytes(reported2, checkout)

    compiles, compile_detail = _compile_check(checkout, diff_path2, target)
    applies, apply_detail = _apply_check(checkout, diff_path2)
    drive.seam_checks.extend(
        [
            check("candidate-compiles", compiles, "py_compile over the candidate", compile_detail),
            check(
                "diff-applies-cleanly",
                applies,
                "git apply --check onto the frozen base",
                apply_detail,
            ),
            check(
                "repaired-content-in-diff",
                _content_in_diff(diff_bytes, str(params["repaired_edit"]), base_target),
                "the repaired bytes ride the collected diff",
                f"diff_digest={reported2['diff_digest']} source={reported2['source']}",
            ),
        ]
    )
    drive.evidence.update(
        {
            "driver": "test_repair",
            "brief": brief,
            "checkpoint_ids": [],
            "diff_digest": reported2["diff_digest"],
            "collector_source": reported2["source"],
            "failed_first_attempt_diff_digest": reported["diff_digest"],
            "meta": _read_lane_meta(checkout),
        }
    )
    drive.artifacts.extend(
        [
            ("candidate.diff", diff_path2),
            ("first-attempt.diff", diff_path),
            ("vendor-events.jsonl", eventlog),
        ]
    )
    drive.recount = (checkout, diff_path2)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_cold_reinstall(setup: TaskSetup) -> TaskDrive:
    """t-02: the workspace is COLD-REINSTALLED (prior state wiped, fresh
    checkout) before the ordinary change runs; the collector must still
    resolve the generation."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    # A prior workspace with prior local state — then the cold reinstall.
    prior, _prior_base = pe_helpers.make_checkout(root, "prior-workspace")
    (prior / ".forge").mkdir(exist_ok=True)
    (prior / "forge-output").mkdir(exist_ok=True)
    shutil.rmtree(prior)
    checkout, base = pe_helpers.make_checkout(root, "workspace")
    cold = not (checkout / ".forge").exists() and not (checkout / "forge-output").exists()
    drive.seam_checks.append(
        check(
            "cold-start-proven",
            cold,
            "no .forge or forge-output state exists before the lane starts",
            f".forge={(checkout / '.forge').exists()} forge-output={(checkout / 'forge-output').exists()}",
        )
    )
    eventlog = root / "vendor-events.jsonl"
    attempt, outcome = _lane_attempt(
        checkout, setup.env, actions=list(params["actions"]), eventlog=eventlog
    )
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"lane failed: {outcome.stderr[-2000:]}")
    reported = _collect(checkout, setup.work_id, base)
    diff_bytes = _diff_bytes(reported, checkout)
    diff_path = Path(reported["diff_path"])

    expected_files = [str(action["path"]) for action in params["actions"]]
    files_ok = all(name.encode() in diff_bytes for name in expected_files)
    applies, apply_detail = _apply_check(checkout, diff_path)
    drive.seam_checks.extend(
        [
            check(
                "files-present",
                files_ok,
                f"the diff adds/edits {expected_files}",
                f"diff_digest={reported['diff_digest']}",
            ),
            check("diff-applies-cleanly", applies, "git apply --check", apply_detail),
        ]
    )
    drive.evidence.update(
        {
            "driver": "cold_reinstall",
            "brief": str(params["brief"]),
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
            "checkpoint_ids": [],
        }
    )
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.recount = (checkout, diff_path)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_adaptive_idle(setup: TaskSetup) -> TaskDrive:
    """t-03: the adaptive control path attached over real HTTP with ZERO
    pending commands — the honest empty steering journal beside a
    completed ordinary change."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    checkout, base = pe_helpers.make_checkout(root, "workspace")
    eventlog = root / "vendor-events.jsonl"
    base_target = _base_content(checkout, str(params["actions"][0]["path"]))
    attempt, outcome = _lane_attempt(
        checkout, setup.env, actions=list(params["actions"]), eventlog=eventlog
    )
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"lane failed: {outcome.stderr[-2000:]}")
    meta = _read_lane_meta(checkout)
    journal = meta.get("steering_journal")
    drive.seam_checks.append(
        check(
            "steering-attached-idle",
            isinstance(journal, list) and journal == [],
            "the steering journal is an honest EMPTY list (attached, nothing routed)",
            f"steering_journal={journal!r}",
        )
    )
    reported = _collect(checkout, setup.work_id, base)
    diff_bytes = _diff_bytes(reported, checkout)
    diff_path = Path(reported["diff_path"])
    target = str(params["actions"][0]["path"])
    drive.seam_checks.append(
        check(
            "edit-landed",
            _content_in_diff(diff_bytes, str(params["actions"][0]["content"]), base_target),
            f"the {target} change rides the collected diff",
            f"diff_digest={reported['diff_digest']}",
        )
    )
    applies, apply_detail = _apply_check(checkout, diff_path)
    drive.seam_checks.append(
        check("diff-applies-cleanly", applies, "git apply --check", apply_detail)
    )
    drive.evidence.update(
        {
            "driver": "adaptive_idle",
            "brief": str(params["brief"]),
            "steering_journal": journal,
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
            "checkpoint_ids": [],
        }
    )
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.recount = (checkout, diff_path)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_question_first(setup: TaskSetup) -> TaskDrive:
    """t-04/t-05: an ambiguous or requirement-missing brief.  CORRECT
    behavior is asking: the REAL discovery stage raises the question and
    waits (zero vendor turns, zero edits), the operator's answer lands
    as a durable ``answer`` command, and only then the edit runs."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    checkout, base = pe_helpers.make_checkout(root, "workspace")
    eventlog = root / "vendor-events.jsonl"
    question_id = str(params["question_id"])

    # The REAL discovery record over the task's snapshot identity.
    snapshot_digest = hashlib.sha256(
        json.dumps(pe_helpers.tracked_baseline(checkout), sort_keys=True).encode()
    ).hexdigest()
    discovery = (
        DiscoveryRun(
            discovery_id=f"{setup.work_id}:discovery",
            work_id=setup.work_id,
            snapshot_set_digest=snapshot_digest,
        )
        .start()
        .record_evidence(f"read:{params.get('target_file', 'src/app.py')}@{base[:12]}")
        .raise_question(question_id)
    )
    _decision_pending(drive.operator_decisions, question_id, str(params["question_text"]))

    # Attempt 1 — the waiting turn: asking IS the outcome. No vendor, no edits.
    waiting = AttemptLog(
        attempt_id="",
        wall_seconds=0.0,
        note="waiting_question: no runner held while the human answered",
    )
    drive.attempts.append(waiting)
    drive.seam_checks.extend(
        [
            check(
                "question-before-edits",
                _vendor_event_count(eventlog) == 0
                and pe_helpers._git(checkout, "status", "--porcelain").stdout.strip() == "",
                "zero vendor events and a byte-identical checkout on the waiting attempt",
                f"events={_vendor_event_count(eventlog)}",
            ),
            check(
                "discovery-waiting",
                discovery.status == "waiting_question" and question_id in discovery.open_questions,
                "the discovery record waits on the named question",
                f"status={discovery.status} open={list(discovery.open_questions)}",
            ),
        ]
    )
    if params.get("critical"):
        # The missing requirement is CRITICAL: the stage BLOCKS on it (the
        # recorded view) rather than letting the planner invent a default —
        # the waiting record itself stays live for the resolution below.
        blocked_view = discovery.block(f"unresolved CRITICAL question {question_id}")
        drive.seam_checks.append(
            check(
                "blocked-not-defaulted",
                blocked_view.status == "blocked"
                and question_id in blocked_view.open_questions
                and bool(blocked_view.block_reason),
                "the CRITICAL missing requirement BLOCKS instead of defaulting",
                f"status={blocked_view.status} block_reason={blocked_view.block_reason!r}",
            )
        )

    # The operator's answer — a DURABLE answer command, then the resolution.
    drive.interventions.append(kit.InterventionEvent(kind="question_answer", note=question_id))
    service = setup.env.durable_control()
    created = await service.answer(
        setup.work_id, LAB_OPERATOR, question_id, str(params["answer_text"])
    )
    _decision_made(
        drive.operator_decisions,
        question_id,
        str(params["answer_text"]),
        "the durable answer command that unblocked the wait",
    )
    answer_rows = await setup.env.control_rows(kind="answer")
    discovery = discovery.resolve_question(question_id).complete()
    drive.seam_checks.append(
        check(
            "answer-durable",
            created and bool(answer_rows),
            "the answer exists as a control_commands row of kind answer",
            f"rows={answer_rows}",
        )
    )

    # Attempt 2 — the now-unambiguous task.
    attempt, outcome = _lane_attempt(
        checkout,
        setup.env,
        actions=list(params["final_actions"]),
        eventlog=eventlog,
    )
    attempt.note = "post-answer turn"
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"lane failed: {outcome.stderr[-2000:]}")
    reported = _collect(checkout, setup.work_id, base)
    diff_bytes = _diff_bytes(reported, checkout)
    diff_path = Path(reported["diff_path"])
    landed = _content_in_diff(diff_bytes, str(params["final_actions"][0]["content"]))
    drive.seam_checks.append(
        check(
            str(params.get("final_check_name") or "final-edit-landed"),
            landed,
            "the answered task's edit rides the collected diff",
            f"diff_digest={reported['diff_digest']}",
        )
    )
    applies, apply_detail = _apply_check(checkout, diff_path)
    drive.seam_checks.append(
        check("diff-applies-cleanly", applies, "git apply --check", apply_detail)
    )
    drive.evidence.update(
        {
            "driver": "question_first",
            "brief": str(params["brief"]),
            "question": {
                "id": question_id,
                "text": str(params["question_text"]),
                "critical": bool(params.get("critical")),
                "answer": str(params["answer_text"]),
            },
            "discovery": {
                "discovery_id": discovery.discovery_id,
                "status": discovery.status,
                "evidence_bundle": list(discovery.evidence_bundle),
                "snapshot_set_digest": snapshot_digest,
            },
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
            "checkpoint_ids": [],
        }
    )
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.recount = (checkout, diff_path)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_neighbor(setup: TaskSetup) -> TaskDrive:
    """t-06/t-07: the decisive evidence exists ONLY in the read-only
    neighbor; the plan cites it at the exact OID (the kit's citation
    validator over the real neighbor git repo) and the neighbor stays
    untouched."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    checkout, base = pe_helpers.make_checkout(root, "writable")
    neighbor, _neighbor_base = pe_helpers.make_checkout(root, "neighbor")
    neighbor_path = str(params["neighbor_path"])
    (neighbor / neighbor_path).parent.mkdir(parents=True, exist_ok=True)
    (neighbor / neighbor_path).write_text(str(params["neighbor_content"]))
    _git(neighbor, "add", "-A")
    _git(neighbor, "commit", "-q", "-m", "neighbor decision")
    neighbor_head = _git(neighbor, "rev-parse", "HEAD").stdout.strip()
    neighbor_oid = _git(neighbor, "rev-parse", f"HEAD:{neighbor_path}").stdout.strip()

    evidence_id = "neighbor-decision"
    plan_steps = [
        {
            "step_id": "land-constant",
            "action": f"write {params['target_file']} with the cited value",
            "evidence_refs": [evidence_id],
        }
    ]
    evidence = {evidence_id: {"path": neighbor_path, "source_oid": neighbor_oid}}
    violations = validate_plan_citations(plan_steps, evidence)
    plan = structured_plan(
        summary=f"Land the constant from the neighbor decision ({neighbor_path}).",
        steps=plan_steps,
        assumptions=[],
        unknowns=[],
        decision_requests=[],
    )
    _decision_pending(
        drive.operator_decisions,
        "plan-approval",
        f"Does the plan cite the neighbor decision at {neighbor_oid[:12]}…?",
    )
    _decision_made(
        drive.operator_decisions,
        "plan-approval",
        "approved",
        f"citations resolve to lab/neighbor-contracts@{neighbor_oid[:12]}…:{neighbor_path}",
    )
    drive.seam_checks.append(
        check(
            "citations-resolve",
            not violations,
            "every plan citation resolves to repository + OID + path",
            f"violations={violations} oid={neighbor_oid}",
        )
    )

    eventlog = root / "vendor-events.jsonl"
    attempt, outcome = _lane_attempt(
        checkout,
        setup.env,
        actions=[
            {
                "op": "write",
                "path": str(params["target_file"]),
                "content": str(params["target_content"]),
            }
        ],
        eventlog=eventlog,
    )
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"lane failed: {outcome.stderr[-2000:]}")
    reported = _collect(checkout, setup.work_id, base)
    diff_path = Path(reported["diff_path"])
    diff_bytes = _diff_bytes(reported, checkout)

    status_after = _git(neighbor, "status", "--porcelain").stdout.strip()
    head_after = _git(neighbor, "rev-parse", "HEAD").stdout.strip()
    drive.seam_checks.extend(
        [
            check(
                "neighbor-read-only",
                status_after == "" and head_after == neighbor_head,
                "the neighbor stays read-only (clean status, unchanged head)",
                f"status={status_after!r} head_unchanged={head_after == neighbor_head}",
            ),
            check(
                str(params.get("value_check_name") or "value-from-contract"),
                _content_in_diff(diff_bytes, str(params["target_content"]))
                and str(params["cited_value"]) in str(params["target_content"]),
                f"the landed constant equals the cited value {params['cited_value']}",
                f"diff_digest={reported['diff_digest']}",
            ),
        ]
    )
    applies, apply_detail = _apply_check(checkout, diff_path)
    drive.seam_checks.append(
        check("diff-applies-cleanly", applies, "git apply --check", apply_detail)
    )
    drive.evidence.update(
        {
            "driver": "neighbor",
            "brief": str(params["brief"]),
            "neighbor": {
                "repo": "lab/neighbor-contracts",
                "path": neighbor_path,
                "source_oid": neighbor_oid,
                "head_before": neighbor_head,
                "head_after": head_after,
            },
            "plan_digest": plan_digest(plan),
            "plan": plan,
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
            "checkpoint_ids": [],
        }
    )
    drive.touched_repos = ["lab/checkout", "lab/neighbor-contracts"]
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_intervention_steer(setup: TaskSetup) -> TaskDrive:
    """t-08: the operator's guidance recorded as a DURABLE steer command
    BEFORE the lane runs; the lane (steering attached over real HTTP,
    fast poll) carries the steering journal; the deliverable reflects
    the steered path."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    checkout, base = pe_helpers.make_checkout(root, "workspace")
    eventlog = root / "vendor-events.jsonl"

    _decision_pending(
        drive.operator_decisions,
        "mid-run-steer",
        f"Should the lane be steered mid-run: {params['steer_text']!r}?",
    )
    service = setup.env.durable_control()
    result = await service.steer(
        setup.work_id,
        LAB_OPERATOR,
        str(params["steer_text"]),
        idempotency_key=f"lab:{setup.work_id}:steer",
    )
    if result.get("status") != "accepted":
        raise PilotRunnerError(f"the durable steer was not accepted: {result}")
    drive.interventions.append(kit.InterventionEvent(kind="steer", note=str(params["steer_text"])))
    _decision_made(
        drive.operator_decisions,
        "mid-run-steer",
        "steered",
        f"durable command {result.get('command_id')}",
    )
    steer_rows = await setup.env.control_rows(kind="steer")
    drive.seam_checks.append(
        check(
            "steer-durable",
            bool(steer_rows),
            "the steer exists as a control_commands row",
            f"rows={steer_rows}",
        )
    )

    attempt, outcome = _lane_attempt(
        checkout,
        setup.env,
        actions=list(params["actions"]),
        eventlog=eventlog,
        poll_seconds="0.01",
    )
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"lane failed: {outcome.stderr[-2000:]}")
    meta = _read_lane_meta(checkout)
    journal = meta.get("steering_journal")
    kinds = [entry.get("kind") for entry in journal] if isinstance(journal, list) else None
    drive.seam_checks.append(
        check(
            "journal-names-the-steer",
            isinstance(journal, list) and any(kind == "steer" for kind in (kinds or [])),
            "the lane meta's steering journal holds an entry of kind steer",
            f"journal={journal!r}",
        )
    )
    reported = _collect(checkout, setup.work_id, base)
    diff_bytes = _diff_bytes(reported, checkout)
    diff_path = Path(reported["diff_path"])
    steered_file = str(params["actions"][0]["path"])
    drive.seam_checks.append(
        check(
            "steered-path-landed",
            _content_in_diff(diff_bytes, str(params["actions"][0]["content"])),
            f"the steered artifact ({steered_file}) rides the candidate",
            f"diff_digest={reported['diff_digest']}",
        )
    )
    applies, apply_detail = _apply_check(checkout, diff_path)
    drive.seam_checks.append(
        check("diff-applies-cleanly", applies, "git apply --check", apply_detail)
    )
    vendor_events = (
        [json.loads(line) for line in eventlog.read_text().splitlines() if line.strip()]
        if eventlog.is_file()
        else []
    )
    steer_entry = next((entry for entry in (journal or []) if entry.get("kind") == "steer"), None)
    vendor_saw = any(event.get("kind") == "turn_steered" for event in vendor_events)
    drive.evidence.update(
        {
            "driver": "intervention_steer",
            "brief": str(params["brief"]),
            "steer_text": str(params["steer_text"]),
            "steer_command": result,
            "steering_journal": journal,
            "steer_delivery_state": {
                "journal_outcome": (steer_entry or {}).get("outcome"),
                "journal_reason": (steer_entry or {}).get("reason", ""),
                "vendor_saw_turn_steered": vendor_saw,
                "note": (
                    "the scripted vendor's turn completes faster than any poll cadence, so a "
                    "mid-turn delivery LANDING is not demonstrable at lab scope — what is real "
                    "and recorded: the durable steer row, the drain over real HTTP, and the "
                    "journal's honest delivery state (an error outcome is recorded, never faked)"
                ),
            },
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
            "checkpoint_ids": [],
        }
    )
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.recount = (checkout, diff_path)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_intervention_pause_resume(setup: TaskSetup) -> TaskDrive:
    """t-09: the operator PAUSES mid-run (durable command + verified
    checkpoint over real HTTP), then RESUMES; the final candidate
    carries both legs (AT-01 seam outcome)."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    env = setup.env
    checkout, base = pe_helpers.make_checkout(root, "workspace")
    eventlog = root / "vendor-events.jsonl"
    token = env.token()

    _decision_pending(
        drive.operator_decisions, "pause", "Should the run pause and checkpoint its WIP?"
    )
    pre_pause_started = time.monotonic()
    pe_helpers.run_vendor_once(checkout, list(params["pre_pause_actions"]), eventlog)
    service = env.durable_control()
    await service.pause(setup.work_id, LAB_OPERATOR, f"lab:{setup.work_id}:pause")
    checkpoint_id = await pe_helpers.upload_wip_checkpoint(
        env.control_url, checkout, setup.work_id, token, base
    )
    pause_rows = await env.control_rows(kind="pause")
    drive.interventions.append(kit.InterventionEvent(kind="pause", note=checkpoint_id))
    _decision_made(
        drive.operator_decisions, "pause", "paused", f"checkpoint {checkpoint_id} verified+uploaded"
    )
    waiting = AttemptLog(
        attempt_id="",
        wall_seconds=time.monotonic() - pre_pause_started,
        note="pre-pause WIP leg + pause checkpoint (no model turn held while paused)",
    )
    waiting.tool_events = _vendor_event_count(eventlog)
    drive.attempts.append(waiting)
    drive.seam_checks.append(
        check(
            "checkpoint-verified",
            bool(checkpoint_id),
            "the pause's capture receipt is verified and its artifact id recorded",
            f"checkpoint_id={checkpoint_id} pause_rows={len(pause_rows)}",
        )
    )

    _decision_pending(
        drive.operator_decisions, "resume", "Resume the paused work into a new runner?"
    )
    resumed_ok = await pe_helpers.record_resume_command(env.factory, setup.work_id)
    _decision_made(drive.operator_decisions, "resume", "resumed", "durable resume command recorded")
    drive.interventions.append(kit.InterventionEvent(kind="resume", note="cross-runner resume"))
    drive.seam_checks.append(
        check(
            "resume-command-recorded",
            resumed_ok is True,
            "the resume command row exists",
            f"{resumed_ok}",
        )
    )

    resumed, resumed_base = pe_helpers.make_checkout(root, "resumed-workspace")
    attempt, outcome = _lane_attempt(
        resumed,
        env,
        actions=list(params["resumed_actions"]),
        eventlog=eventlog,
        resume="1",
    )
    attempt.note = "resumed turn (required restore)"
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"resumed lane failed: {outcome.stderr[-2000:]}")
    reported = _collect(resumed, setup.work_id, resumed_base)
    diff_bytes = _diff_bytes(reported, resumed)
    diff_path = Path(reported["diff_path"])
    pointer = json.loads((resumed / ".forge" / "workspace-generation").read_text())
    pre_ok = _content_in_diff(diff_bytes, str(params["pre_pause_actions"][0]["content"]))
    post_ok = _content_in_diff(diff_bytes, str(params["resumed_actions"][0]["content"]))
    drive.seam_checks.extend(
        [
            check(
                "resume-restored-exact-wip",
                pointer.get("checkpoint_id") == checkpoint_id and pre_ok,
                "the generation pointer names the checkpoint and the restored bytes ride the candidate",
                f"pointer_checkpoint={pointer.get('checkpoint_id')} restored_bytes={pre_ok}",
            ),
            check(
                "collector-resolved-generation",
                reported["source"] == "generation" and reported["zero_change"] is False,
                "the shipped collector resolved the ACTIVE workspace generation (AT-01)",
                f"source={reported['source']} zero_change={reported['zero_change']}",
            ),
            check(
                "both-legs-in-diff",
                pre_ok and post_ok,
                "the diff carries the pre-pause AND resumed-turn edits",
                f"diff_digest={reported['diff_digest']}",
            ),
        ]
    )
    applies, apply_detail = _apply_check(resumed, diff_path)
    drive.seam_checks.append(
        check("diff-applies-cleanly", applies, "git apply --check", apply_detail)
    )
    drive.evidence.update(
        {
            "driver": "intervention_pause_resume",
            "brief": str(params["brief"]),
            "checkpoint_ids": [checkpoint_id],
            "workspace_generation_pointer": pointer,
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
            "meta": _read_lane_meta(resumed),
        }
    )
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.recount = (resumed, diff_path)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_runner_loss_retry(setup: TaskSetup) -> TaskDrive:
    """t-10: the AT-02 seam outcome — a bootstrap death before any
    useful work, then the authenticated /retry through the REAL
    GitHubRunService whose dispatch travels real HTTP to the fake
    native server (whose ledger records lane_resume_mode=fresh)."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    native = _spawn_native(root)
    from forge.integrations.github import GitHubClient, GitHubRepositoryReader

    class _StaticTokens:
        def __init__(self, token: str) -> None:
            self._token = token

        async def token(self) -> str:
            return self._token

        async def invalidate(self) -> None:  # the client's 401 re-auth hook
            return None

    client = GitHubClient(
        base_url=native.base_url, token_provider=_StaticTokens("lab-native-token")
    )
    try:
        reader = GitHubRepositoryReader(client, pe.PE_OWNER, pe.PE_REPO_NAME)
        assert setup.env.factory is not None
        service = pe.make_service(setup.env.factory, client, reader)
        native.seed_issue(42, str(params["issue_title"]), str(params["issue_body"]))
        run_id = await pe_helpers.start_run(service, 42)
        await pe_helpers.go(service, run_id, 42)
        first_dispatch_count = len(native.dispatches())

        # The bootstrap death (PE-2's honesty note: the worker's terminal
        # journal, as the reconciler would write it).
        async with setup.env.factory() as session:
            run = await session.get(FlowRun, run_id)
            assert run is not None
            run.status = "failed"
            run.status_reason = "harness_infrastructure: harness_bootstrap_failed (node setup)"
            await session.commit()
        await service.handle_retry(
            project_id=pe_helpers.PROJECT_ID,
            issue_number=42,
            note_text=f"/retry {run_id}",
            author_username="alice",
            delivery_id=f"{setup.work_id}-retry-1",
        )
        _decision_made(
            drive.operator_decisions,
            "retry-after-loss",
            "retry (fresh)",
            "no candidate and no checkpoint existed — the committed baseline",
        )

        dispatches = native.dispatches()
        branch = f"forge/42/{run_id[:8]}"
        resumed_inputs = [entry["inputs"] for entry in dispatches if entry["ref"] == branch]
        run = await pe_helpers.get_run(setup.env.factory, run_id)
        continuation = (run.evidence or {}).get("continuation") or {}
        drive.seam_checks.extend(
            [
                check(
                    "ledger-carries-fresh-retry",
                    len(dispatches) == first_dispatch_count + 1
                    and bool(resumed_inputs)
                    and resumed_inputs[-1].get("lane_resume_mode") == "fresh",
                    "exactly one NEW dispatch whose inputs carry lane_resume_mode=fresh",
                    f"dispatches={len(dispatches)} last_inputs={resumed_inputs[-1] if resumed_inputs else None}",
                ),
                check(
                    "no-checkpoint-prerequisite",
                    resumed_inputs
                    and not [key for key in resumed_inputs[-1] if "checkpoint" in key],
                    "no checkpoint ref keys rode the retry dispatch",
                    f"inputs_keys={sorted(resumed_inputs[-1]) if resumed_inputs else None}",
                ),
                check(
                    "decision-evidence-bound",
                    continuation.get("mode_selected") == "fresh"
                    and continuation.get("no_checkpoint_baseline") is True,
                    "the run's continuation evidence names fresh + no_checkpoint_baseline",
                    f"continuation={continuation}",
                ),
                check(
                    "client-stayed-on-modeled-surface",
                    native.unknown_paths() == [],
                    "the real client never fell off the modeled API",
                    f"unknown_paths={native.unknown_paths()}",
                ),
            ]
        )
        drive.evidence.update(
            {
                "driver": "runner_loss_retry",
                "brief": str(params["brief"]),
                "run_id": run_id,
                "dispatch_ledger": dispatches,
                "continuation_evidence": continuation,
                "native_comments": native.comments(),
                "checkpoint_ids": [],
            }
        )
        with (setup.records_dir / "dispatch-ledger.json").open("w", encoding="utf-8") as handle:
            json.dump(dispatches, handle, indent=2)
    finally:
        await client.aclose()
        native.close()
    drive.touched_repos = ["acme/forge-pe"]
    drive.attempts.append(
        AttemptLog(attempt_id="", wall_seconds=0.0, note="bootstrap death: no vendor turn ever ran")
    )
    drive.attempts.append(
        AttemptLog(attempt_id="", wall_seconds=0.0, note="retry dispatch: fresh mode demanded")
    )
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_resource_revocation(setup: TaskSetup) -> TaskDrive:
    """t-11: the AT-03 seam outcome — a rotted referenced blob at the
    authority halts the required continuation with ZERO vendor events,
    then the operator's FRESH retry completes the task."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    env = setup.env
    import httpx

    checkout, base = pe_helpers.make_checkout(root, "workspace")
    eventlog = root / "vendor-events.jsonl"
    token = env.token()
    pe_helpers.run_vendor_once(checkout, list(params["pre_pause_actions"]), eventlog)
    checkpoint_id = await pe_helpers.upload_wip_checkpoint(
        env.control_url, checkout, setup.work_id, token, base
    )
    await pe_helpers.record_resume_command(env.factory, setup.work_id)

    # ROT one referenced blob at the authority (the revoked resource).
    served = httpx.get(
        env.control.url(f"/lane/checkpoints/{setup.work_id}"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    ).json()
    first_blob = next(iter(served["blobs"]))
    blob_path = env.store_dir / first_blob[:2] / first_blob
    blob_path.write_bytes(b"rotted bytes - not the addressed content\n")

    resumed, _resumed_base = pe_helpers.make_checkout(root, "resumed-workspace")
    halted, outcome = _lane_attempt(
        resumed,
        env,
        actions=list(params["retry_actions"]),
        eventlog=eventlog,
        resume="1",
    )
    halted.note = "required resume halted: wip_restore_failed (zero vendor events)"
    drive.attempts.append(halted)
    meta = _read_lane_meta(resumed)
    kinds = (
        [json.loads(line)["kind"] for line in eventlog.read_text().splitlines() if line.strip()]
        if eventlog.is_file()
        else []
    )
    post_pause = kinds[kinds.index("vendor_edits") + 1 :] if "vendor_edits" in kinds else kinds
    checkout_clean = pe_helpers._git(resumed, "status", "--porcelain").stdout.strip() == ""
    drive.seam_checks.extend(
        [
            check(
                "halt-was-loud",
                outcome.returncode != 0
                and "wip_restore_failed" in outcome.stderr
                and meta.get("exit") == "failed",
                "the resumed lane exits non-zero with wip_restore_failed and a failed meta",
                f"returncode={outcome.returncode} exit={meta.get('exit')} terminal={meta.get('terminal_reason')}",
            ),
            check(
                "zero-vendor-events-after-pause",
                post_pause == [],
                "no vendor event follows the pause leg",
                f"post_pause={post_pause}",
            ),
            check(
                "nothing-published",
                checkout_clean and not (resumed / "forge-output").exists(),
                "the checkout stays clean and nothing was published",
                f"clean={checkout_clean}",
            ),
        ]
    )

    _decision_pending(
        drive.operator_decisions,
        "recovery-after-revocation",
        "The required resume cannot restore (revoked blob). Retry fresh?",
    )
    _decision_made(
        drive.operator_decisions,
        "recovery-after-revocation",
        "retry fresh",
        "the rotted checkpoint is unrecoverable; the committed baseline is the honest restart",
    )
    retry_checkout, retry_base = pe_helpers.make_checkout(root, "retry-workspace")
    attempt, outcome2 = _lane_attempt(
        retry_checkout,
        env,
        actions=list(params["retry_actions"]),
        eventlog=eventlog,
    )
    attempt.note = "operator-authorized fresh retry"
    drive.attempts.append(attempt)
    if outcome2.returncode != 0:
        raise PilotRunnerError(f"fresh retry lane failed: {outcome2.stderr[-2000:]}")
    reported = _collect(retry_checkout, setup.work_id, retry_base)
    diff_bytes = _diff_bytes(reported, retry_checkout)
    diff_path = Path(reported["diff_path"])
    drive.seam_checks.append(
        check(
            "fresh-retry-completed",
            _content_in_diff(diff_bytes, str(params["retry_actions"][0]["content"])),
            "the retry attempt's collector reports the intended edit",
            f"diff_digest={reported['diff_digest']} source={reported['source']}",
        )
    )
    applies, apply_detail = _apply_check(retry_checkout, diff_path)
    drive.seam_checks.append(
        check("diff-applies-cleanly", applies, "git apply --check", apply_detail)
    )
    drive.evidence.update(
        {
            "driver": "resource_revocation",
            "brief": str(params["brief"]),
            "checkpoint_ids": [checkpoint_id],
            "rotted_blob": first_blob,
            "halt_terminal_reason": meta.get("terminal_reason"),
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
        }
    )
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.recount = (retry_checkout, diff_path)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


async def drive_cross_runner_resume(setup: TaskSetup) -> TaskDrive:
    """t-12: the AT-01 seam outcome — the first runner's WIP checkpointed
    over real HTTP, a SECOND runner restoring the exact bytes into a new
    generation, the shipped collector capturing changed+new+deleted."""
    drive = TaskDrive(accepted=False)
    params = setup.params
    root = setup.env.root
    env = setup.env
    original, base = pe_helpers.make_checkout(root, "first-runner")
    eventlog = root / "vendor-events.jsonl"
    token = env.token()

    first_started = time.monotonic()
    pe_helpers.run_vendor_once(original, list(params["pre_pause_actions"]), eventlog)
    first = AttemptLog(
        attempt_id="",
        wall_seconds=time.monotonic() - first_started,
        note="first runner's pre-pause WIP leg (checkpointed; no usage receipt exists for --once)",
    )
    first.tool_events = _vendor_event_count(eventlog)
    drive.attempts.append(first)
    checkpoint_id = await pe_helpers.upload_wip_checkpoint(
        env.control_url, original, setup.work_id, token, base
    )
    await pe_helpers.record_resume_command(env.factory, setup.work_id)
    drive.interventions.append(kit.InterventionEvent(kind="resume", note="cross-runner resume"))
    _decision_made(
        drive.operator_decisions,
        "cross-runner-continuation",
        "resume on a second runner",
        f"checkpoint {checkpoint_id} admitted the continuation",
    )

    resumed, resumed_base = pe_helpers.make_checkout(root, "second-runner")
    attempt, outcome = _lane_attempt(
        resumed,
        env,
        actions=list(params["resumed_actions"]),
        eventlog=eventlog,
        resume="1",
    )
    attempt.note = "second runner: required restore + resumed turn"
    drive.attempts.append(attempt)
    if outcome.returncode != 0:
        raise PilotRunnerError(f"resumed lane failed: {outcome.stderr[-2000:]}")
    reported = _collect(resumed, setup.work_id, resumed_base)
    diff_bytes = _diff_bytes(reported, resumed)
    diff_path = Path(reported["diff_path"])
    pointer = json.loads((resumed / ".forge" / "workspace-generation").read_text())

    overwritten = {str(action["path"]) for action in params["resumed_actions"]}
    restored_ok = True
    for action in params["pre_pause_actions"]:
        rel = str(action["path"])
        if rel in overwritten:
            continue  # the resumed turn's final content replaces it (its own check)
        if str(action.get("op")) == "delete":
            restored_ok = (
                restored_ok and b"deleted file mode" in diff_bytes and rel.encode() in diff_bytes
            )
        else:
            restored_ok = restored_ok and _content_in_diff(
                diff_bytes, str(action.get("content") or "")
            )
    new_file = str(params["pre_pause_actions"][1]["path"]).encode()
    deleted_file = str(params["pre_pause_actions"][2]["path"]).encode()
    resumed_ok = _content_in_diff(diff_bytes, str(params["resumed_actions"][0]["content"]))
    first_intact = (original / str(params["pre_pause_actions"][0]["path"])).read_text() == str(
        params["pre_pause_actions"][0]["content"]
    )
    second_clean = pe_helpers._git(resumed, "status", "--porcelain").stdout.strip() == ""
    drive.seam_checks.extend(
        [
            check(
                "restored-bytes-in-candidate",
                restored_ok
                and new_file in diff_bytes
                and deleted_file in diff_bytes
                and b"deleted file mode" in diff_bytes,
                "changed, new AND deleted files from the restored WIP ride the diff",
                f"diff_digest={reported['diff_digest']}",
            ),
            check(
                "resumed-turn-in-candidate",
                resumed_ok,
                "the resumed turn's own edit rides the diff",
                f"source={reported['source']}",
            ),
            check(
                "generation-pointer-names-checkpoint",
                pointer.get("checkpoint_id") == checkpoint_id,
                "the resumed checkout's generation pointer names the checkpoint id",
                f"pointer={pointer}",
            ),
            check(
                "collector-resolved-generation",
                reported["source"] == "generation" and reported["zero_change"] is False,
                "the shipped collector resolved the ACTIVE workspace generation (AT-01)",
                f"source={reported['source']} zero_change={reported['zero_change']}",
            ),
            check(
                "both-checkouts-intact",
                first_intact and second_clean,
                "the first checkout keeps its WIP bytes; the resumed checkout is clean at base",
                f"first_intact={first_intact} second_clean={second_clean}",
            ),
        ]
    )
    applies, apply_detail = _apply_check(resumed, diff_path)
    drive.seam_checks.append(
        check("diff-applies-cleanly", applies, "git apply --check", apply_detail)
    )
    drive.evidence.update(
        {
            "driver": "cross_runner_resume",
            "brief": str(params["brief"]),
            "checkpoint_ids": [checkpoint_id],
            "workspace_generation_pointer": pointer,
            "diff_digest": reported["diff_digest"],
            "collector_source": reported["source"],
            "meta": _read_lane_meta(resumed),
        }
    )
    drive.artifacts.extend([("candidate.diff", diff_path), ("vendor-events.jsonl", eventlog)])
    drive.recount = (resumed, diff_path)
    drive.accepted = all(entry["ok"] for entry in drive.seam_checks)
    return drive


DRIVERS: dict[str, Any] = {
    "test_repair": drive_test_repair,
    "cold_reinstall": drive_cold_reinstall,
    "adaptive_idle": drive_adaptive_idle,
    "question_first": drive_question_first,
    "neighbor": drive_neighbor,
    "intervention_steer": drive_intervention_steer,
    "intervention_pause_resume": drive_intervention_pause_resume,
    "runner_loss_retry": drive_runner_loss_retry,
    "resource_revocation": drive_resource_revocation,
    "cross_runner_resume": drive_cross_runner_resume,
}


# ---------------------------------------------------------------------------
# One task: environment, driver, tracker record, evidence
# ---------------------------------------------------------------------------


async def drive_one_task(
    task: kit.PilotTask,
    params: Mapping[str, Any],
    records_root: Path,
    *,
    registry: Mapping[str, Any] | None = None,
) -> tuple[kit.TaskRecord, dict[str, Any]]:
    """Run ONE task for real and fold it into its tracker record.

    Everything a partner pilot would record is captured here — honest
    setup minutes, plan corrections, manual rescue notes, measured
    latency, per-attempt usage in the #276 ledger shape (unknown costs
    labeled), interventions, reviewer minutes and the operator's
    decision points with their PENDING states.
    """
    registry = registry or DRIVERS
    kind = str(params.get("kind") or "")
    driver = registry.get(kind)
    if driver is None:
        raise PilotRunnerError(f"{task.task_id}: no driver for scenario kind {kind!r}")
    work_id = f"lab-{task.task_id}"
    task_records = records_root / task.task_id
    task_records.mkdir(parents=True, exist_ok=True)
    task_started = time.monotonic()
    started_iso = _now_iso()

    with tempfile.TemporaryDirectory(prefix=f"lab-pilot-{task.task_id}-") as scratch:
        root = Path(scratch)
        needs_runs = kind == "runner_loss_retry"
        async with LabEnvironment(root, work_id, with_durable_runs=needs_runs) as env:
            with _env_scope(**env.lane_env_scope()):
                setup_started = time.monotonic()
                setup_obj = TaskSetup(
                    task=task, params=params, env=env, records_dir=task_records, work_id=work_id
                )
                try:
                    drive = await driver(setup_obj)
                except BlockScenario as blocked:
                    reason = blocked.reason.strip()
                    if not reason.startswith("blocked:"):
                        reason = f"blocked: {reason}"
                    record = kit.TaskRecord(
                        task_id=task.task_id,
                        started_at=started_iso,
                        setup_minutes=_minutes(setup_started),
                        blocked=reason,
                    )
                    evidence = {
                        "task_id": task.task_id,
                        "scenario": task.scenario,
                        "driver": kind,
                        "blocked": reason,
                        "operator_decisions": [
                            {
                                "point": "task-inclusion",
                                "state": "DECIDED",
                                "decision": "recorded blocked — the task stays in every denominator",
                                "decided_by": LAB_OPERATOR,
                                "at": _now_iso(),
                            }
                        ],
                    }
                    _write_task_evidence(task_records, task, evidence, [], record)
                    return record, evidence
                drive.setup_minutes = _minutes(setup_started)

        # The reviewer leg: an independent RECOUNT of the candidate (a
        # fresh clone + git apply --check over the RECORDED diff — never
        # the agent's self-report), then the coverage fold and the
        # acceptance decision.  Review minutes are the measured time of
        # exactly this work.
        review_started = time.monotonic()
        if drive.recount is not None:
            base_checkout, diff_path = drive.recount
            recount_ok, recount_detail = _apply_check(base_checkout, diff_path)
            drive.seam_checks.append(
                check(
                    "reviewer-recount",
                    recount_ok,
                    "the reviewer independently re-applied the recorded diff onto a fresh clone",
                    recount_detail,
                )
            )
        verification_names = [verify.name for verify in task.verification]
        check_names = [entry["name"] for entry in drive.seam_checks]
        unmapped = [name for name in verification_names if name not in check_names]
        if unmapped:
            drive.seam_checks.append(
                check(
                    "verification-coverage",
                    False,
                    "every contracted verification check ran",
                    f"unmapped verification checks: {unmapped}",
                )
            )
        else:
            drive.seam_checks.append(
                check(
                    "verification-coverage",
                    True,
                    "every contracted verification check ran",
                    f"{len(verification_names)} contracted checks all executed",
                )
            )
        accepted = drive.accepted and all(entry["ok"] for entry in drive.seam_checks)
        _decision_pending(
            drive.operator_decisions,
            "acceptance",
            "Does the candidate satisfy the task's contracted verification?",
        )
        _decision_made(
            drive.operator_decisions,
            "acceptance",
            "accepted" if accepted else "rejected",
            "; ".join(
                f"{entry['name']}={'ok' if entry['ok'] else 'FAILED'}"
                for entry in drive.seam_checks
            ),
        )
        drive.review_minutes = _minutes(review_started)
        if drive.attempts:
            drive.completion_latency_minutes = round((time.monotonic() - task_started) / 60.0, 4)
        _name_attempts(drive, task.task_id)
        usage_rows = [_usage_row(attempt) for attempt in drive.attempts]
        record = kit.TaskRecord(
            task_id=task.task_id,
            started_at=started_iso,
            accepted=accepted,
            human_code_change=False,
            setup_minutes=drive.setup_minutes,
            plan_corrections=drive.plan_corrections,
            plan_correction_notes=tuple(drive.plan_correction_notes),
            completion_latency_minutes=drive.completion_latency_minutes,
            attempts=tuple(
                kit.AttemptUsage(
                    attempt_id=log.attempt_id, accepted=index == len(drive.attempts) and accepted
                )
                for index, log in enumerate(drive.attempts, start=1)
            ),
            interventions=tuple(drive.interventions),
            review_minutes=drive.review_minutes,
            touched_repos=tuple(drive.touched_repos),
        )
        evidence = dict(drive.evidence)
        evidence.update(
            {
                "task_id": task.task_id,
                "scenario": task.scenario,
                "driver": kind,
                "seam_checks": drive.seam_checks,
                "attempts": [
                    {
                        "attempt_id": log.attempt_id,
                        "note": log.note,
                        "wall_seconds": round(log.wall_seconds, 3),
                    }
                    for log in drive.attempts
                ],
                "usage_ledger": usage_rows,
                "operator_decisions": drive.operator_decisions,
                "interventions": [
                    {"kind": event.kind, "note": event.note} for event in drive.interventions
                ],
                "checkpoint_ids": list(drive.evidence.get("checkpoint_ids") or []),
                "evidence_class": EVIDENCE_CLASS,
            }
        )
        for name, source in drive.artifacts:
            target = task_records / name
            if source.is_file():
                shutil.copy2(source, target)
                evidence.setdefault("artifact_files", []).append(name)
        _write_task_evidence(task_records, task, evidence, usage_rows, record)
    return record, evidence


def _write_task_evidence(
    task_records: Path,
    task: kit.PilotTask,
    evidence: dict[str, Any],
    usage_rows: list[dict[str, Any]],
    record: kit.TaskRecord,
) -> None:
    document = {
        "schema": "forge.lab-pilot.task-evidence/1",
        "task": task.as_document(),
        "record": kit._task_record_document(record),
        "usage_ledger": usage_rows,
        **evidence,
    }
    (task_records / "task-evidence.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# The pilot: all tasks, stop conditions, records, report
# ---------------------------------------------------------------------------


async def run_pilot(
    plan: kit.PilotPlan,
    drivers: Mapping[str, dict[str, Any]],
    out_dir: Path,
    *,
    registry: Mapping[str, Any] | None = None,
    only: tuple[str, ...] = (),
) -> kit.PilotTracker:
    """Execute the pilot: onboarding BEFORE any task, every task in plan
    order, stop conditions enforced after each task (any violation
    preserves diagnostics and halts the remaining tasks)."""
    tracker = kit.PilotTracker(plan.spec, kit.record_onboarding(plan.spec))
    records_root = out_dir / RECORDS_DIRNAME
    records_root.mkdir(parents=True, exist_ok=True)
    stop_reason = ""
    for task in plan.tasks:
        if only and task.task_id not in only:
            continue
        if stop_reason:
            print(f"HALTED before {task.task_id}: {stop_reason}", file=sys.stderr)
            break
        record, _evidence = await drive_one_task(
            task, drivers[task.task_id], records_root, registry=registry
        )
        tracker.record_task(record)
        outcome = (
            f"{record.task_id}: accepted={record.accepted}"
            f"{' BLOCKED=' + record.blocked if record.blocked else ''}"
            f" attempts={len(record.attempts)}"
            f" interventions={len(record.interventions)}"
        )
        print(outcome, flush=True)
        violations = tracker.violations
        if violations:
            head = violations[0]
            stop_reason = (
                f"stop rule: {head.kind} ({head.detail}) — diagnostics preserved; "
                "the remaining tasks are not run"
            )
            tracker.preserve_diagnostics(stop_reason)
            print(stop_reason, file=sys.stderr)
    finalize_pilot(plan, tracker, out_dir, stop_reason=stop_reason)
    return tracker


def finalize_pilot(
    plan: kit.PilotPlan,
    tracker: kit.PilotTracker,
    out_dir: Path,
    *,
    stop_reason: str = "",
) -> kit.PilotReport:
    """Write records FIRST (the durable evidence), then the decision and
    the report — the order that makes the report rebuildable from the
    records alone, byte-identically."""
    records_root = out_dir / RECORDS_DIRNAME
    records_root.mkdir(parents=True, exist_ok=True)
    snapshot = tracker.snapshot_document()
    (records_root / "tracker-snapshot.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    run_state = {
        "schema": RUN_STATE_SCHEMA,
        "plan_id": plan.plan_id,
        "spec_digest": plan.spec.frozen_digest,
        "stop_reason": stop_reason,
        "as_of": plan.spec.decision_date.isoformat(),
        "note": (
            "records precede the decision by construction; rebuild with "
            "--rebuild-from to reproduce report.json byte-identically"
        ),
    }
    (records_root / "run-state.json").write_text(
        json.dumps(run_state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    decision = kit.evaluate_stop(tracker, plan.spec, as_of=plan.spec.decision_date)
    report = kit.build_pilot_report(plan, tracker, decision)
    report.write(out_dir / REPORT_FILE)
    write_bounding(plan, out_dir)
    return report


def write_bounding(plan: kit.PilotPlan, out_dir: Path) -> None:
    """The evidence-class bounding document — the honest frame beside the
    kit report: what ran for real, what did not, the live blockers and
    the deviations from the partner-pilot shape."""
    bounding = {
        "schema": BOUNDING_SCHEMA,
        "pilot_id": plan.spec.pilot_id,
        "plan_id": plan.plan_id,
        "evidence_class": EVIDENCE_CLASS,
        "provenance": "lab-operational-pilot",
        "what_ran_for_real": list(REAL_SEAMS),
        "what_did_not_run": list(NOT_REAL),
        "live_blockers_cross_ref": dict(LIVE_BLOCKERS),
        "deviations_from_the_partner_pilot_shape": [
            "the environment is the forge lab, not a design-partner customer — no buyer/user problem was agreed with a customer and no fee was paid (the demand test is unexecuted)",
            "the window is the execution day, not 4-8 weeks — no J-curve and no adoption learning exist to measure",
            "the baseline is an AUTHORED lab baseline (the operator's planned manual time), not the partner's own frozen pre-pilot numbers — cycle_time_vs_baseline is bounded by that and never headlined",
            "acceptance is the operator's mechanical independent verification over seam artifacts, not partner engineering review",
            "the 2-of-3 verdict below is a LAB-SCOPE verdict about operating the seams; it says nothing about partner outcomes",
        ],
        "criteria_verdict_scope": (
            "the 2-of-3 verdict in report.json is bounded to evidence_class lab-operational; "
            "the cost criterion is unjudgeable by construction (no paid models) and is named, not failed"
        ),
        "next_step": (
            "a paid design-partner pilot on the qualified profile, once the #268 live blockers "
            "clear (control-plane version pin, budget caps, the GitLab lane-resume dispatch parity)"
        ),
        "spec_digest": plan.spec.frozen_digest,
    }
    (out_dir / BOUNDING_FILE).write_text(
        json.dumps(bounding, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def rebuild_report(out_dir: Path, *, spec_path: Path, tasks_path: Path) -> kit.PilotReport:
    """Rebuild report.json from the records alone — the determinism proof."""
    plan, _drivers = load_lab_plan(spec_path, tasks_path)
    records_root = out_dir / RECORDS_DIRNAME
    snapshot = json.loads((records_root / "tracker-snapshot.json").read_text(encoding="utf-8"))
    run_state = json.loads((records_root / "run-state.json").read_text(encoding="utf-8"))
    tracker = kit.tracker_from_snapshot(
        plan.spec, snapshot, stop_reason=str(run_state.get("stop_reason") or "")
    )
    return finalize_pilot(
        plan, tracker, out_dir, stop_reason=str(run_state.get("stop_reason") or "")
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--out", type=Path, default=None, help="output directory for the pilot")
    source.add_argument(
        "--rebuild-from",
        type=Path,
        default=None,
        help="rebuild report.json from an existing records directory (the determinism proof)",
    )
    parser.add_argument("--spec", type=Path, default=None, help="the pilot spec document")
    parser.add_argument("--tasks", type=Path, default=None, help="the task definitions document")
    parser.add_argument(
        "--only",
        type=str,
        default="",
        help="comma-separated task ids to run (debugging; the plan stays the full set)",
    )
    args = parser.parse_args(argv)

    default_dir = default_plan_dir()
    spec_path = args.spec or default_dir / SPEC_FILE
    tasks_path = args.tasks or default_dir / TASKS_FILE
    if (args.spec is None) != (args.tasks is None):
        parser.error("--spec and --tasks go together")

    if args.rebuild_from is not None:
        report = rebuild_report(args.rebuild_from, spec_path=spec_path, tasks_path=tasks_path)
        print(f"rebuilt {args.rebuild_from / REPORT_FILE} (digest {report.digest[:12]}…)")
        return 0

    out_dir = args.out or default_dir
    plan, drivers = load_lab_plan(spec_path, tasks_path)
    only = tuple(name.strip() for name in args.only.split(",") if name.strip())
    print(
        f"lab pilot {plan.spec.pilot_id}: {len(plan.tasks)} tasks, "
        f"evidence_class={EVIDENCE_CLASS}, out={out_dir}",
        flush=True,
    )
    tracker = asyncio.run(run_pilot(plan, drivers, out_dir, only=only))
    metrics = tracker.metrics()
    print(
        "\nlab pilot complete: "
        f"tasks={metrics.tasks_total} accepted={metrics.tasks_accepted} "
        f"autonomy={metrics.autonomy_rate} interventions={metrics.intervention_rate} "
        f"cost={metrics.total_cost_usd} (unknown is honest) "
        f"cycle_vs_baseline={metrics.cycle_time_vs_baseline}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
