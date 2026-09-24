#!/usr/bin/env python3
"""R37-12 (issue #293) — the staged design-partner pilot LADDER runner.

``forge.adaptive.pilot_ladder`` froze the staged contract (contract →
one-observed-task → supervised-batch → operational-sample), the
customer-baseline measurement format and the decision-record machinery.
This script EXECUTES one stage of that ladder honestly:

``--stage contract``
    Records the contract stage locally: the contract validates, its
    digest is frozen, the customer state is stated.  No lab, no spend.

``--stage one-observed-task``
    The machinery leg.  A read-only PREFLIGHT runs first (the lab
    aligned to its intended profile per the #287/#289 inventory; the
    model gateway reachable; the numerical budget caps configured; the
    per-stage spend cap present and bounded).  When any arm is unmet
    the stage record is written ``pending-lab`` with the EXACT unmet
    preconditions and their re-check paths — never a silent skip, never
    a pretend run.  When every arm passes, ONE task runs end-to-end
    through the qualified path (the lab's real services + the real
    model, spend bounded by the stage cap, disposable project), and
    every observable lands in the stage record: attempts, acceptance by
    the INDEPENDENT ORACLE (never PR creation), manual rescues,
    timings, usage receipts (spend unknown until reported — never zero).

``--stage supervised-batch`` / ``--stage operational-sample``
    The partner stages.  They fail CLOSED while the named external
    customer, the code owner's task-set approval or the observed
    baseline is missing — recorded ``pending-partner`` with the named
    prerequisites.  The machinery cannot fabricate a customer.

Every run re-writes ``ladder-state.json`` (the replayable ladder
state), ``stages/<stage>.json`` (the stage's records) and
``report.json`` (the report with the honest status and the UNFILLED
decision-review template).  Exit code 0 means the stage RAN or was
honestly recorded pending; exit 2 means a usage/refusal error.

Usage::

    uv run python scripts/run_pilot_stage.py --stage contract
    uv run python scripts/run_pilot_stage.py --stage one-observed-task
    uv run python scripts/run_pilot_stage.py --stage supervised-batch
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from forge.adaptive import pilot_ladder as ladder_kit  # noqa: E402
from forge.adaptive.pilot_ladder import (  # noqa: E402
    LADDER_STAGES,
    STATUS_EXECUTED,
    STATUS_PENDING_LAB,
    STATUS_PENDING_PARTNER,
    STAGE_CONTRACT,
    STAGE_ONE_OBSERVED_TASK,
    STAGE_OPERATIONAL_SAMPLE,
    Precondition,
    StageRecord,
    UsageReceipt,
)

DEFAULT_DIR = REPO_ROOT / "evaluation" / "pilot" / "partner-pilot-v1"
CONTRACT_FILE = "contract.json"
TASKS_FILE = "tasks.json"
STATE_FILE = "ladder-state.json"
REPORT_FILE = "report.json"
STAGES_DIRNAME = "stages"

#: The lab preflight defaults (the #287 inventory's own targets).  The
#: gateway arm probes LiteLLM's LIVENESS surface (the cheap, documented
#: reachability check) — the deep per-model /health fold is already part
#: of the app's own /health ("litellm": ok) inside the aligned verdict.
DEFAULT_APP_HEALTH_URL = "http://localhost:8420/health"
DEFAULT_GATEWAY_HEALTH_URL = "http://localhost:4000/health/liveliness"
DEFAULT_GITLAB_PROJECT_ID = 68

#: The preflight arms whose unmet outcomes make a stage ``pending-lab``.
LAB_ARM_CHECKS = ("lab-aligned", "gateway-reachable", "caps-configured")
#: The partner prerequisites no lab work can substitute for.
PARTNER_ARM_CHECKS = ("customer-named", "task-set-approved", "baseline-observed")

#: The runbook that resolves every alignment mismatch (per the inventory).
ALIGNMENT_RUNBOOK = "docs/operations/lab-alignment-runbook.md"

_OBSERVED_TASK_MARKER = "def pilot_stage1_probe"
_OBSERVED_TASK_BRANCH_FILE = "pilot_probe.py"


class PilotStageError(RuntimeError):
    """The stage runner hit a condition it refuses to paper over."""


def _load_inventory_lab():
    """Load ``scripts/inventory_lab.py`` by path (the #287 probe layer)."""
    if "inventory_lab" in sys.modules:
        return sys.modules["inventory_lab"]
    spec = importlib.util.spec_from_file_location(
        "inventory_lab", REPO_ROOT / "scripts" / "inventory_lab.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise PilotStageError("could not load scripts/inventory_lab.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["inventory_lab"] = module
    spec.loader.exec_module(module)
    return module


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minutes(since: float) -> float:
    return round((time.monotonic() - since) / 60.0, 4)


# ---------------------------------------------------------------------------
# The task set
# ---------------------------------------------------------------------------


def load_task_set(path: Path | str) -> dict[str, Any]:
    """Load + structurally validate the staged task set document."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        raise PilotStageError("task set document is not an object")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise PilotStageError("task set needs a non-empty tasks list")
    seen: set[str] = set()
    per_stage: dict[str, list[dict[str, Any]]] = {stage: [] for stage in LADDER_STAGES}
    for entry in tasks:
        if not isinstance(entry, Mapping):
            raise PilotStageError("task entry is not an object")
        task_id = str(entry.get("task_id") or "")
        stage = str(entry.get("stage") or "")
        if not task_id.strip():
            raise PilotStageError("task needs a non-empty task_id")
        if task_id in seen:
            raise PilotStageError(f"duplicate task id {task_id!r}")
        seen.add(task_id)
        if stage not in LADDER_STAGES:
            raise PilotStageError(
                f"{task_id}: unknown stage {stage!r}; the ladder is {list(LADDER_STAGES)}"
            )
        if float(entry.get("spend_cap_usd") or 0.0) <= 0:
            raise PilotStageError(f"{task_id}: needs a positive spend_cap_usd")
        per_stage[stage].append(dict(entry))
    observed = per_stage[STAGE_ONE_OBSERVED_TASK]
    if len(observed) != 1:
        raise PilotStageError(
            f"the one-observed-task stage holds EXACTLY one task, got {len(observed)}"
        )
    sample = per_stage[STAGE_OPERATIONAL_SAMPLE]
    if not 12 <= len(sample) <= 20:
        raise PilotStageError(
            f"the operational sample is a 12-20 task slice (a planning proposal, not a "
            f"statistical calibration), got {len(sample)}"
        )
    return dict(document)


# ---------------------------------------------------------------------------
# The lab preflight: aligned / gateway / caps / spend-cap arms
# ---------------------------------------------------------------------------


def lab_preflight(
    root: Path,
    contract: ladder_kit.LearningContract,
    stage: str,
    *,
    max_spend_usd: float,
    probe: Any = None,
    gateway_url: str = DEFAULT_GATEWAY_HEALTH_URL,
    gitlab_project_id: int = DEFAULT_GITLAB_PROJECT_ID,
    lane_venv: Path | None = None,
    remaining_budget_usd: float | None = None,
) -> tuple[list[Precondition], dict[str, Any]]:
    """The read-only preflight whose unmet arms make a stage ``pending-lab``.

    Reuses the #287 inventory probe layer verbatim (HTTP + read-only
    podman) so the arms are OBSERVED, never inferred: the lab's
    compatibility verdict against the intended profile, the model
    gateway's health, the numerical budget caps and the stage spend cap
    against the remaining budget.
    """
    inventory_lab = _load_inventory_lab()
    probe = probe or inventory_lab.LabProbe(app_health_url=DEFAULT_APP_HEALTH_URL)
    observations: dict[str, Any] = {}
    try:
        document = inventory_lab.run_inventory(
            probe,
            root,
            stages=inventory_lab.STAGES,
            gitlab_project_id=gitlab_project_id,
            lane_venv=lane_venv or inventory_lab.DEFAULT_LANE_VENV,
        )
    except Exception as exc:  # noqa: BLE001 — an unreachable inventory is a refusal
        document = {
            "stamp": inventory_lab.INVENTORY_STAMP,
            "error": f"inventory refused: {exc}",
            "compatibility_verdict": {"verdict": "unverified", "checks": []},
        }
    observations["inventory"] = document

    verdict = str(document.get("compatibility_verdict", {}).get("verdict") or "unverified")
    mismatched = [
        str(check.get("check"))
        for check in document.get("compatibility_verdict", {}).get("checks", [])
        if str(check.get("result")) != "match"
    ]
    preconditions = [
        Precondition(
            check="lab-aligned",
            met=verdict == "aligned",
            reason=(
                "the lab matches its intended profile on every check"
                if verdict == "aligned"
                else f"compatibility verdict {verdict!r}; unmet checks: {mismatched or 'inventory refused'}"
            ),
            resolution=f"resolve each mismatch per {ALIGNMENT_RUNBOOK}, then re-run this stage",
        )
    ]

    try:
        probe.http_get_json(gateway_url)
        preconditions.append(
            Precondition(
                check="gateway-reachable",
                met=True,
                reason=f"the model gateway answered at {gateway_url}",
            )
        )
    except Exception as exc:  # noqa: BLE001 — unreachable is the observation
        preconditions.append(
            Precondition(
                check="gateway-reachable",
                met=False,
                reason=f"the model gateway at {gateway_url} is unreachable: {exc}",
                resolution="bring the gateway up (the litellm container) and re-run",
            )
        )

    caps = (
        document.get("stages", {}).get("caps", {}).get("caps_present_and_numerical")
        if isinstance(document.get("stages"), Mapping)
        else None
    )
    preconditions.append(
        Precondition(
            check="caps-configured",
            met=caps is True,
            reason=(
                "numerical budget caps present on the app/worker containers"
                if caps is True
                else "budget caps absent or non-numerical on the app/worker containers "
                "(FORGE_BUDGET_PROFILES / FORGE_LANE_BUDGET_SECONDS)"
            ),
            resolution=f"configure the caps per {ALIGNMENT_RUNBOOK} §4 and recreate the containers",
        )
    )

    stage_cap = contract.stage_spend_caps_usd.get(stage)
    budget_ok = stage_cap is not None and max_spend_usd <= stage_cap
    if remaining_budget_usd is not None:
        budget_ok = budget_ok and max_spend_usd <= remaining_budget_usd
    if budget_ok:
        within = f"the requested bound {max_spend_usd} USD is within the stage cap {stage_cap} USD"
        if remaining_budget_usd is not None:
            within += f" and the remaining budget {remaining_budget_usd} USD"
        budget_reason = within
    else:
        budget_reason = (
            f"the requested bound {max_spend_usd} USD exceeds the stage cap {stage_cap} USD "
            f"or the remaining budget {remaining_budget_usd} USD — an uncapped paid task is refused"
        )
    preconditions.append(
        Precondition(
            check="stage-spend-cap-bounded",
            met=budget_ok,
            reason=budget_reason,
            resolution="lower --max-spend-usd or raise the CONTRACT's stage cap (a new contract)",
        )
    )
    observations["preflight_arms"] = [entry.as_document() for entry in preconditions]
    return preconditions, observations


# ---------------------------------------------------------------------------
# The live lab boundary (used ONLY after the preflight passes)
# ---------------------------------------------------------------------------


class LiveLabBoundary:
    """The lab's live surfaces — the app's run API, GitLab and the gateway.

    Everything the aligned execution path touches goes through here so
    the machinery is drivable with fakes and every observable is a wire
    fact, never an inference.  Credentials come from ``forge.config``
    (never parsed out of ``.env`` by hand).
    """

    def __init__(
        self,
        *,
        app_url: str = "http://localhost:8420",
        gitlab_url: str = "",
        gitlab_token: str = "",
        api_read_token: str = "",
    ) -> None:
        self.app_url = app_url.rstrip("/")
        self.gitlab_url = gitlab_url.rstrip("/")
        self.gitlab_token = gitlab_token
        self.api_read_token = api_read_token

    @classmethod
    def from_settings(cls, app_url: str) -> LiveLabBoundary:
        from forge.config import Settings

        settings = Settings()
        api_token = getattr(settings, "FORGE_API_READ_TOKEN", None)
        return cls(
            app_url=app_url,
            gitlab_url=str(settings.GITLAB_URL),
            gitlab_token=settings.GITLAB_TOKEN.get_secret_value(),
            api_read_token=(api_token.get_secret_value() if api_token is not None else ""),
        )

    async def _app_get(self, path: str) -> Any:
        import httpx

        headers = {"Authorization": f"Bearer {self.api_read_token}"} if self.api_read_token else {}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(f"{self.app_url}{path}", headers=headers)
            response.raise_for_status()
            return response.json()

    async def _gitlab(self, method: str, path: str, *, json_body: Any = None) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method,
                f"{self.gitlab_url}/api/v4{path}",
                headers={"PRIVATE-TOKEN": self.gitlab_token},
                json=json_body,
            )
            response.raise_for_status()
            return response.json() if response.content else {}

    async def file_issue(self, project_id: int, title: str, body: str) -> int:
        """File the stage task as an issue mentioning @forge; return its iid."""
        created = await self._gitlab(
            "POST",
            f"/projects/{project_id}/issues",
            json_body={"title": title, "description": body},
        )
        return int(created["iid"])

    async def add_issue_note(self, project_id: int, issue_iid: int, body: str) -> None:
        await self._gitlab(
            "POST",
            f"/projects/{project_id}/issues/{issue_iid}/notes",
            json_body={"body": body},
        )

    async def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        document = await self._app_get(f"/runs?limit={limit}")
        return list(document.get("runs") or [])

    async def get_run(self, run_id: str) -> dict[str, Any]:
        return dict(await self._app_get(f"/runs/{run_id}"))

    async def get_file_content(self, project_id: int, path: str, ref: str) -> str:
        import base64

        encoded = path.replace("/", "%2F")
        document = await self._gitlab(
            "GET", f"/projects/{project_id}/repository/files/{encoded}?ref={ref}"
        )
        return base64.b64decode(str(document.get("content", ""))).decode("utf-8", "replace")


_TERMINAL_STATUSES = frozenset({"ready_for_human", "failed", "blocked", "cancelled", "completed"})


async def execute_observed_task(
    boundary: LiveLabBoundary,
    task: Mapping[str, Any],
    *,
    project_id: int,
    max_spend_usd: float,
    max_wall_seconds: float = 900.0,
    approver_note: str = "",
) -> dict[str, Any]:
    """Run ONE observed task end-to-end on the lab's live surfaces.

    The flow: file the task as an @forge issue on the DISPOSABLE project
    → wait for the control plane to pick it up → approve the plan (the
    /go note) → wait for the terminal state inside the wall-clock bound
    → evaluate acceptance with the INDEPENDENT ORACLE (the verification
    evidence on the candidate sha + the contracted content check on the
    candidate branch).  PR/MR creation is RECORDED and never counts as
    acceptance.  Spend is what the wire reports — unknown until then,
    never zero.
    """
    started = time.monotonic()
    setup_started = time.monotonic()
    task_id = str(task.get("task_id") or "obs-01")
    brief = str(task.get("brief") or "")
    title = f"[partner-pilot stage-1] {task.get('title') or task_id}"
    attempts: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    issue_iid = await boundary.file_issue(project_id, title, f"@forge {brief}")
    checks.append(
        {
            "name": "task-filed",
            "ok": True,
            "observed": f"issue !{issue_iid} on project {project_id}",
        }
    )
    run: dict[str, Any] | None = None
    deadline = time.monotonic() + max_wall_seconds
    while time.monotonic() < deadline:
        runs = await boundary.list_runs(limit=20)
        run = next((entry for entry in runs if entry.get("issue_iid") == issue_iid), None)
        if run is not None:
            break
        await asyncio.sleep(5.0)
    if run is None:
        raise PilotStageError(
            f"no run appeared for issue !{issue_iid} within {max_wall_seconds}s — the "
            "webhook/dispatch seam did not carry the task; the stage is NOT executed"
        )
    setup_minutes = _minutes(setup_started)

    if str(run.get("status")) == "waiting_approval":
        if not approver_note.strip():
            raise PilotStageError(
                "the run waits for plan approval and no --approver-note was given — a "
                "human gate cannot be self-approved by the runner; the stage is NOT executed"
            )
        await boundary.add_issue_note(
            project_id, issue_iid, approver_note.format(run_id=run.get("id"))
        )
        checks.append(
            {
                "name": "plan-approved-by-named-approver",
                "ok": True,
                "observed": f"approver note landed on run {run.get('id')}",
            }
        )

    wait_started = time.monotonic()
    while time.monotonic() < deadline:
        run = await boundary.get_run(str(run.get("id")))
        if str(run.get("status")) in _TERMINAL_STATUSES:
            break
        await asyncio.sleep(10.0)
    wait_minutes = _minutes(wait_started)
    run = run or {}
    status = str(run.get("status"))
    attempts.append(
        {
            "attempt_id": f"{task_id}/live-1",
            "outcome": status,
            "note": "one live task through the qualified path (the lab's real services + the real model)",
        }
    )

    review_started = time.monotonic()
    evidence = dict(run.get("evidence") or {})
    pipeline = dict(evidence.get("pipeline") or {})
    harness = dict(evidence.get("harness") or {})
    candidate_sha = str(pipeline.get("sha") or "")
    pipeline_status = str(pipeline.get("status") or "")
    checks.append(
        {
            "name": "verification-green-on-candidate-sha",
            "ok": pipeline_status == "success" and bool(candidate_sha),
            "observed": f"pipeline={pipeline_status} sha={candidate_sha[:12] or 'none'}",
        }
    )
    branch = str(harness.get("branch") or "")
    content_ok = False
    content_observed = "no candidate branch observed"
    if branch:
        try:
            content = await boundary.get_file_content(
                project_id, _OBSERVED_TASK_BRANCH_FILE, branch
            )
            content_ok = _OBSERVED_TASK_MARKER in content and "2026" in content
            content_observed = (
                f"{_OBSERVED_TASK_MARKER!r} present on {branch}"
                if content_ok
                else f"marker absent on {branch}"
            )
        except Exception as exc:  # noqa: BLE001 — a failed read is the observation
            content_observed = f"file read failed: {exc}"
    checks.append(
        {
            "name": "contracted-content-on-candidate-branch",
            "ok": content_ok,
            "observed": content_observed,
        }
    )
    checks.append(
        {
            "name": "run-reached-ready-for-human",
            "ok": status == "ready_for_human",
            "observed": f"status={status} status_reason={run.get('status_reason')!r}",
        }
    )
    accepted = all(entry["ok"] for entry in checks)
    review_minutes = _minutes(review_started)
    return {
        "task_id": task_id,
        "issue_iid": issue_iid,
        "run": {
            "id": run.get("id"),
            "status": status,
            "status_reason": run.get("status_reason"),
            "mr_iid": run.get("mr_iid"),
            "commit_cycle": run.get("commit_cycle"),
        },
        "attempts": attempts,
        "checks": checks,
        "accepted": accepted,
        "timings": {
            "setup_minutes": setup_minutes,
            "wait_minutes": wait_minutes,
            "review_minutes": review_minutes,
            "wall_minutes": _minutes(started),
        },
        "spend_receipts": [
            UsageReceipt(
                attempt_id=f"{task_id}/live-1",
                source="lab-live/model-gateway",
                spend_usd=None,
            )
        ],
        "spend_note": (
            "spend unknown until the gateway reports the run's cost — never zero; the "
            f"stage stays bounded by the {max_spend_usd} USD cap either way"
        ),
        "pr_creation_recorded_but_not_acceptance": bool(run.get("mr_iid")),
    }


# ---------------------------------------------------------------------------
# Stage recording
# ---------------------------------------------------------------------------


def record_contract_stage(ladder: ladder_kit.PilotLadder) -> StageRecord:
    """Complete the contract stage: validated, digest-frozen, customer stated."""
    gate = ladder.gate(STAGE_CONTRACT)
    if not gate.allowed:
        raise PilotStageError(
            f"the contract stage's gate refuses: {[entry.check for entry in gate.unmet]}"
        )
    record = StageRecord(
        stage=STAGE_CONTRACT,
        status=STATUS_EXECUTED,
        recorded_at=_now_iso(),
        acceptance={
            "accepted": True,
            "method": "contract-validation",
            "checks": [
                {
                    "name": "contract-validates",
                    "ok": True,
                    "observed": f"digest {ladder.contract_digest[:12]}…",
                },
                {
                    "name": "customer-state-stated",
                    "ok": True,
                    "observed": (
                        "pending-recruitment (honest)"
                        if ladder.contract.customer.is_pending
                        else ladder.contract.customer.name
                    ),
                },
            ],
            "note": "the contract validates and its digest is frozen; the customer state "
            "is stated, never faked",
        },
        stage_complete=True,
        notes=["the learning contract exists and is frozen; no spend, no tasks"],
    )
    ladder.record_stage(record)
    return record


def pending_record(
    stage: str,
    status: str,
    unmet: list[Precondition] | tuple[Precondition, ...],
    *,
    task_refs: tuple[str, ...] = (),
    observations: Mapping[str, Any] | None = None,
) -> StageRecord:
    """A pending stage record: the EXACT unmet preconditions, re-checkable."""
    notes = [f"re-check by re-running: uv run python scripts/run_pilot_stage.py --stage {stage}"]
    if observations:
        notes.append(
            "preflight observations retained in the ladder state's stage records "
            "(read-only; nothing was restarted or written on the lab)"
        )
    return StageRecord(
        stage=stage,
        status=status,
        recorded_at=_now_iso(),
        task_refs=task_refs,
        unmet_preconditions=tuple(unmet),
        notes=tuple(notes),
    )


def executed_record(
    stage: str,
    outcome: Mapping[str, Any],
    *,
    manual_rescues: int = 0,
    rescue_notes: tuple[str, ...] = (),
) -> StageRecord:
    """An executed stage record from the live outcome document."""
    return StageRecord(
        stage=stage,
        status=STATUS_EXECUTED,
        recorded_at=_now_iso(),
        task_refs=(str(outcome.get("task_id") or ""),),
        acceptance={
            "accepted": bool(outcome.get("accepted")),
            "method": "independent-oracle",
            "checks": list(outcome.get("checks") or []),
            "run": dict(outcome.get("run") or {}),
            "pr_creation_recorded_but_not_acceptance": bool(
                outcome.get("pr_creation_recorded_but_not_acceptance")
            ),
            "note": "acceptance is the independent oracle (verification green on the "
            "candidate sha + the contracted content check) — NEVER PR creation",
        },
        attempts=tuple(dict(entry) for entry in outcome.get("attempts") or ()),
        manual_rescues=manual_rescues,
        rescue_notes=rescue_notes,
        timings=dict(outcome.get("timings") or {}),
        usage_receipts=tuple(outcome.get("spend_receipts") or ()),
        notes=(str(outcome.get("spend_note") or ""),),
        stage_complete=bool(outcome.get("accepted")),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_ladder(out_dir: Path, contract: ladder_kit.LearningContract) -> ladder_kit.PilotLadder:
    state_path = out_dir / STATE_FILE
    if state_path.is_file():
        ladder = ladder_kit.PilotLadder.from_document(
            json.loads(state_path.read_text(encoding="utf-8"))
        )
        if ladder.contract_digest != contract.frozen_digest:
            raise PilotStageError(
                "the persisted ladder state is bound to a different contract "
                f"({ladder.contract_digest[:12]}… != {contract.frozen_digest[:12]}…) — "
                "a contract change is a NEW ladder"
            )
        return ladder
    return ladder_kit.PilotLadder(contract)


def persist(ladder: ladder_kit.PilotLadder, out_dir: Path) -> None:
    """Write the state, the stage records and the report (deterministic)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    state = ladder.state_document()
    (out_dir / STATE_FILE).write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    stages_root = out_dir / STAGES_DIRNAME
    stages_root.mkdir(parents=True, exist_ok=True)
    for stage in LADDER_STAGES:
        records = ladder.stage_records(stage)
        if not records:
            continue
        document = {
            "schema": "forge.partner-pilot.stage-records/1",
            "stage": stage,
            "status": ladder.stage_status(stage),
            "records": [record.as_document() for record in records],
        }
        (stages_root / f"{stage}.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    report = ladder_kit.build_partner_pilot_report(ladder)
    (out_dir / REPORT_FILE).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/run_pilot_stage.py",
        description=(
            "R37-12 (#293): execute ONE stage of the design-partner pilot ladder "
            "honestly — pending-lab / pending-partner records carry the exact unmet "
            "preconditions; an executed stage-1 runs one live task bounded by the "
            "stage's spend cap on the aligned lab."
        ),
    )
    parser.add_argument("--stage", required=True, choices=list(LADDER_STAGES))
    parser.add_argument("--out", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--contract", type=Path, default=None)
    parser.add_argument("--tasks", type=Path, default=None)
    parser.add_argument(
        "--max-spend-usd",
        type=float,
        default=1.0,
        help="the stage's spend bound (default 1.00 USD; refused above the contract's stage cap)",
    )
    parser.add_argument("--app-health-url", default=DEFAULT_APP_HEALTH_URL)
    parser.add_argument("--app-url", default="http://localhost:8420")
    parser.add_argument("--gateway-url", default=DEFAULT_GATEWAY_HEALTH_URL)
    parser.add_argument("--gitlab-project", type=int, default=DEFAULT_GITLAB_PROJECT_ID)
    parser.add_argument(
        "--max-wall-seconds", type=float, default=900.0, help="the live task's wall-clock bound"
    )
    parser.add_argument(
        "--approver-note",
        default="",
        help="the /go note body (with {run_id}) a named approver posts when the plan gate holds; "
        "without it a waiting run is NOT self-approved",
    )
    args = parser.parse_args(argv)

    contract_path = args.contract or args.out / CONTRACT_FILE
    tasks_path = args.tasks or args.out / TASKS_FILE
    try:
        contract = ladder_kit.LearningContract.load(contract_path)
        task_set = load_task_set(tasks_path)
        ladder = load_ladder(args.out, contract)
    except (OSError, ValueError, ladder_kit.PilotLadderError) as exc:
        print(f"run_pilot_stage: REFUSED: {exc}", file=sys.stderr)
        return 2

    stage_tasks = [entry for entry in task_set["tasks"] if str(entry.get("stage")) == args.stage]
    task_refs = tuple(str(entry.get("task_id")) for entry in stage_tasks)

    if args.stage == STAGE_CONTRACT:
        record = record_contract_stage(ladder)
        print(f"stage contract: executed (digest {ladder.contract_digest[:12]}…)")
        persist(ladder, args.out)
        print(f"wrote {args.out / STATE_FILE} and {args.out / REPORT_FILE}")
        return 0

    if args.stage == STAGE_ONE_OBSERVED_TASK:
        preconditions, observations = lab_preflight(
            REPO_ROOT,
            contract,
            args.stage,
            max_spend_usd=args.max_spend_usd,
            gateway_url=args.gateway_url,
            gitlab_project_id=args.gitlab_project,
            remaining_budget_usd=ladder.budget_remaining_usd,
        )
        unmet = [entry for entry in preconditions if not entry.met]
        if unmet:
            record = pending_record(args.stage, STATUS_PENDING_LAB, unmet, task_refs=task_refs)
            ladder.record_stage(record)
            persist(ladder, args.out)
            print(f"stage one-observed-task: PENDING-LAB — {len(unmet)} unmet precondition(s):")
            for entry in unmet:
                print(f"  - {entry.check}: {entry.reason}")
                print(f"      resolution: {entry.resolution}")
            print(f"wrote {args.out / STATE_FILE} (re-check by re-running this command)")
            return 0
        gate = ladder.gate(args.stage)
        if not gate.allowed:
            ordering = [entry for entry in gate.unmet if entry.check.startswith("stage-")]
            if ordering:
                print(
                    "run_pilot_stage: REFUSED: the ladder advances in order — run "
                    f"{[entry.check for entry in ordering]} first",
                    file=sys.stderr,
                )
                return 2
            record = pending_record(
                args.stage, STATUS_PENDING_PARTNER, gate.unmet, task_refs=task_refs
            )
            ladder.record_stage(record)
            persist(ladder, args.out)
            print(f"stage one-observed-task: PENDING — {[entry.check for entry in gate.unmet]}")
            return 0
        try:
            boundary = LiveLabBoundary.from_settings(args.app_url)
            outcome = asyncio.run(
                execute_observed_task(
                    boundary,
                    stage_tasks[0],
                    project_id=args.gitlab_project,
                    max_spend_usd=args.max_spend_usd,
                    max_wall_seconds=args.max_wall_seconds,
                    approver_note=args.approver_note,
                )
            )
        except Exception as exc:  # noqa: BLE001 — a live failure is recorded
            unmet_failure = [
                Precondition(
                    check="live-execution",
                    met=False,
                    reason=f"the live task did not complete: {exc}",
                    resolution="the failure is the observation — inspect the run on the lab "
                    "and re-run the stage; nothing is papered over",
                )
            ]
            record = pending_record(
                args.stage, STATUS_PENDING_LAB, unmet_failure, task_refs=task_refs
            )
            ladder.record_stage(record)
            persist(ladder, args.out)
            print(f"stage one-observed-task: PENDING-LAB — live execution failed: {exc}")
            return 0
        record = executed_record(args.stage, outcome)
        ladder.record_stage(record)
        persist(ladder, args.out)
        accepted = outcome.get("accepted")
        print(
            f"stage one-observed-task: EXECUTED — oracle accepted={accepted} "
            f"status={outcome['run'].get('status')} spend={record.spend_usd} (unknown is honest)"
        )
        print(f"wrote {args.out / STATE_FILE} and {args.out / REPORT_FILE}")
        return 0

    # The partner stages: fail CLOSED on the partner prerequisites.
    gate = ladder.gate(args.stage)
    if gate.allowed:
        print(
            f"run_pilot_stage: REFUSED: stage {args.stage!r} is gated for partner execution "
            "under supervision — this runner records pending states and the observed-task "
            "leg only; the partner stages run with the customer present",
            file=sys.stderr,
        )
        return 2
    record = pending_record(args.stage, STATUS_PENDING_PARTNER, gate.unmet, task_refs=task_refs)
    ladder.record_stage(record)
    persist(ladder, args.out)
    print(f"stage {args.stage}: PENDING-PARTNER — unmet prerequisites:")
    for entry in gate.unmet:
        print(f"  - {entry.check}: {entry.reason}")
        print(f"      resolution: {entry.resolution}")
    print(f"wrote {args.out / STATE_FILE} (re-check by re-running this command)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
