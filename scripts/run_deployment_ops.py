#!/usr/bin/env python3
"""R37-20 (issue #301) — qualify the deployment's operating boundaries.

The R36-21 drills (#280) proved the invariants on disposable fixtures.
This runner executes the DEPLOYMENT drills from
:mod:`forge.adaptive.ops_drills` against the ACTUAL aligned lab
(control plane 0.36.0 @ schema 027, numerical budget caps — see
``docs/evaluation/2026-09-24-live-single-writer/alignment-receipts.json``):

- ``topology``      — the deployment DECLARED from read-only ``podman
  inspect`` + ``GET /health`` + the runner inventory: containers, the
  shared /app/data CAS volume, postgres/redis/litellm, runner isolation,
  the observed admission bound. The CAS-semantics statement is explicit:
  the shared VOLUME (not the shared database) is what makes one node's
  bytes visible to both consumers; a replica without the mount is stated
  as OUTSIDE the supported topology, never silently assumed.
- ``occupancy``     — N concurrent dispatch cycles through the app's OWN
  entry (issue → /implement → /go on a DISPOSABLE GitLab project): REAL
  native jobs on the lab runner (id 4, ``unraid``), cancelled job-level
  immediately after the observation; a lost cancel response simulated at
  the client seam; a failed cancel on an already-completed job. Capacity
  never exceeded; queue wait measured SEPARATELY from execution.
- ``backup_restore``— ``backup_store`` over the app's REAL /app/data CAS
  root (read-only snapshot — no container stop) + a read-only
  ``pg_dump`` of the checkpoint metadata; restore into a DISPOSABLE
  target root + a disposable ``forge_ops_restore`` database; pinned
  checkpoints resolve; mismatched halves refuse.
- ``credentials``   — the recorded dispatch envelope of the live run
  (read-only evidence) asserts no control-plane root credential entered
  the model-facing variable set; deny probes observe ACTUAL denial on
  the app's real surfaces.
- ``degraded``      — storage pressure (typed quota refusal), provider
  throttling (a fake 429 lane through the app's own bounded revival
  budget) and the slow control ACK (measured received→applied through
  the app's real lane-control endpoints) — fences never disabled.
- ``rotation``      — the lane-control secret rotated on a DISPOSABLE
  configuration: v2 credentials minted, the old generation refused by
  the real generation-scoped auth path; the production procedure is the
  runbook (docs/operations/deployment-boundaries.md).

READ-ONLY + DISPOSABLE only: no lab container is started, stopped or
recreated; the only writes are (a) the DISPOSABLE GitLab project + its
issues/notes/pipelines (deleted at teardown) and (b) DISPOSABLE local
temp roots + a disposable database (dropped at teardown). Native jobs
are cancelled immediately after the observation (bounded spend).

Usage:

    uv run python scripts/run_deployment_ops.py --out qualification/deployment-ops-2026-09-24.json

Exit codes: 0 every executed drill passed · 1 a drill failed or a
section refused (the report is still written — a refusal is evidence).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import secrets as pysecrets
import subprocess
import sys
import tempfile
import time
import uuid as uuid_mod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from forge.adaptive.ops_drills import (  # noqa: E402
    DEPLOYMENT_DRILL_SCOPE,
    RemoteCycleRecord,
    RemoteDispatchLane,
    build_topology_document,
    drill_credential_isolation,
    drill_degraded_modes,
    drill_remote_occupancy,
    drill_restore_deployment,
    drill_token_rotation,
)
from forge.api_lane_control import lane_control_token  # noqa: E402
from forge.config import Settings  # noqa: E402
from run_live_qualification import _patched_lane_override  # noqa: E402

#: The lab's containers (all read-only probes).
CONTAINERS = ("forge-app", "forge-worker", "forge-postgres", "forge-redis", "forge-litellm")
APP_API = "http://localhost:8420"
LAB_PROJECT_ID = 68
RESTORE_DB = "forge_ops_restore"
STORE_ROOT = REPO_ROOT / "data" / "checkpoints"
LIVE_EVIDENCE = (
    REPO_ROOT / "docs" / "evaluation" / "2026-09-24-live-single-writer" / "live-run-evidence.json"
)
#: The lane template pin + variables copied from the lab project (the
#: same seeded shape the R37-08 live qualification used).
LANE_REF_SHA = "4af6b331f703ac2b662edcedd9e1fc1dd80a5059"
TEMPLATE_REF = "v0.36.0"
TEMPLATE_URL = (
    f"https://raw.githubusercontent.com/forcewake/forge/{TEMPLATE_REF}"
    "/ci/templates/claude-sdk-lane.gitlab-ci.yml"
)
VARIABLES_FROM_LAB = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "FORGE_BOT_READ_TOKEN",
    "FORGE_HARNESS_HTTPS_PROXY",
)
#: A deliberately trivial, bounded task: the plan is one cheap model
#: call and the lane job is cancelled before it does real work.
TASK_BODY = """\
## Task

Append exactly one line containing the word `done` to the file `NOTES.md`
(create the file if it does not exist). Do nothing else.

### Contract

- one appended line, nothing more;
- no other file is touched;
- no git commits.
"""
RUN_ID_RE = re.compile(r"go ([0-9a-f]{32})")

REPORT_STAMP = "forge.deployment.ops/1"


class Refused(Exception):
    """A section's precondition failed — recorded, never retried green."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# Read-only lab probes (everything the runner does to the lab goes through
# these — no container lifecycle, no writes outside disposable targets)
# ---------------------------------------------------------------------------


def podman_json(*args: str) -> Any:
    completed = subprocess.run(
        ["podman", *args, "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise Refused(f"podman {' '.join(args)} failed: {completed.stderr.strip()[:200]}")
    return json.loads(completed.stdout or "null")


def inspect_container(name: str) -> dict[str, Any]:
    """The read-only inspection fields the topology declaration needs."""

    raw = podman_json("inspect", name)
    entry = raw[0] if isinstance(raw, list) and raw else {}
    mounts = {
        str(mount.get("Destination")): str(mount.get("Source"))
        for mount in entry.get("Mounts") or []
    }
    ports = entry.get("NetworkSettings", {}).get("Ports") or {}
    return {
        "image": entry.get("ImageName") or entry.get("Config", {}).get("Image", ""),
        "status": entry.get("State", {}).get("Status", ""),
        "mounts": mounts,
        "ports": ports,
        "env_names": sorted(
            {str(item).partition("=")[0] for item in entry.get("Config", {}).get("Env") or []}
        ),
        "env": {
            str(item).partition("=")[0]: str(item).partition("=")[2]
            for item in entry.get("Config", {}).get("Env") or []
        },
    }


def app_health() -> dict[str, Any]:
    response = httpx.get(f"{APP_API}/health", timeout=15.0)
    response.raise_for_status()
    return response.json()


def psql_select(sql: str) -> list[dict[str, str]]:
    """READ-ONLY database reads through the lab postgres container.

    Only statements that start with SELECT reach the database — the
    guard is in code, on purpose: this runner never writes lab rows.
    """

    statement = sql.strip().rstrip(";").strip()
    if not statement.lower().startswith("select"):
        raise Refused(f"psql_select refuses a non-SELECT statement: {statement[:60]!r}")
    completed = subprocess.run(
        [
            "podman",
            "exec",
            "forge-postgres",
            "psql",
            "-U",
            "forge",
            "-d",
            "forge",
            "-t",
            "-A",
            "-F",
            "\x1f",
            "-c",
            statement,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise Refused(f"read-only psql failed: {completed.stderr.strip()[:200]}")
    rows: list[dict[str, str]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        values = line.split("\x1f")
        rows.append({f"col{index}": value for index, value in enumerate(values)})
    return rows


def gitlab_get(settings: Settings, path: str, **kwargs: Any) -> httpx.Response:
    return httpx.get(
        f"{settings.GITLAB_URL.rstrip('/')}/api/v4{path}",
        headers={"PRIVATE-TOKEN": settings.GITLAB_TOKEN.get_secret_value()},
        timeout=60.0,
        **kwargs,
    )


def gitlab_post(settings: Settings, path: str, **kwargs: Any) -> httpx.Response:
    return httpx.post(
        f"{settings.GITLAB_URL.rstrip('/')}/api/v4{path}",
        headers={"PRIVATE-TOKEN": settings.GITLAB_TOKEN.get_secret_value()},
        timeout=60.0,
        **kwargs,
    )


def gitlab_delete(settings: Settings, path: str, **kwargs: Any) -> httpx.Response:
    return httpx.delete(
        f"{settings.GITLAB_URL.rstrip('/')}/api/v4{path}",
        headers={"PRIVATE-TOKEN": settings.GITLAB_TOKEN.get_secret_value()},
        timeout=60.0,
        **kwargs,
    )


def gitlab_put(settings: Settings, path: str, **kwargs: Any) -> httpx.Response:
    return httpx.put(
        f"{settings.GITLAB_URL.rstrip('/')}/api/v4{path}",
        headers={"PRIVATE-TOKEN": settings.GITLAB_TOKEN.get_secret_value()},
        timeout=60.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Section 1 — the topology declaration (read-only)
# ---------------------------------------------------------------------------


def section_topology(settings: Settings) -> dict[str, Any]:
    health = app_health()
    inspections = {name: inspect_container(name) for name in CONTAINERS}
    runners = gitlab_get(settings, "/runners/all").json()
    budget_profiles = inspections["forge-app"]["env"].get("FORGE_BUDGET_PROFILES", "")
    admission_env = {
        name: value
        for name, value in inspections["forge-app"]["env"].items()
        if name.startswith("FORGE_ADMISSION_")
    }
    document = build_topology_document(
        app_health=health,
        container_inspections={
            name: {
                "image": inspection["image"],
                "status": inspection["status"],
                "mounts": inspection["mounts"],
                "ports": inspection["ports"],
            }
            for name, inspection in inspections.items()
        },
        runners=runners,
        admission_env=admission_env,
        cas_host_root=str(STORE_ROOT),
        budget_caps={
            "FORGE_BUDGET_PROFILES_configured": bool(budget_profiles),
            "FORGE_LANE_BUDGET_SECONDS": inspections["forge-app"]["env"].get(
                "FORGE_LANE_BUDGET_SECONDS", ""
            ),
            "statement": (
                "numerical model/provider budgets configured on both consumers "
                "(trivial/standard/heavy: max_calls, max_tokens, wallclock_s; "
                "per-lane wall clock) — budget/resource uncertainty is never free "
                "capacity or zero spend"
            ),
        },
    )
    log(
        "topology: "
        f"{len(document['observed']['containers'])} containers inspected, "
        f"CAS volume mounted into {document['observed']['cas_volume']['mounted_into']}, "
        f"admission limit {document['observed']['admission']['max_active_per_project']}, "
        f"{len(document['discrepancies'])} discrepancy(ies)"
    )
    return document


# ---------------------------------------------------------------------------
# Section 2 — the disposable project + the real remote-occupancy lane
# ---------------------------------------------------------------------------


class LabRemoteDispatchLane(RemoteDispatchLane):
    """The REAL deployment's dispatch entry, driven natively.

    Every cycle is the app's own flow: a GitLab issue on the DISPOSABLE
    project → the ``@forge /implement`` note (the app plans — one cheap
    model call) → the ``@forge /go`` note (the app reserves an execution
    lease and dispatches a REAL pipeline on runner 4). The drill cancels
    every dispatched job job-level immediately after the observation.
    """

    def __init__(
        self,
        settings: Settings,
        project_id: int,
        *,
        cycles: int,
        slow_ack_delay_s: float = 8.0,
    ) -> None:
        self.settings = settings
        self.project_id = project_id
        self.cycles = cycles
        self.slow_ack_delay_s = slow_ack_delay_s
        self.issues: list[int] = []
        self.runs: dict[int, str] = {}
        #: filled by cycle 0's pause leg — the slow-control-ACK
        #: measurement the degraded section reports.
        self.ack_measurement: dict[str, Any] = {}
        app_env = inspect_container("forge-app")["env"]
        raw_limit = app_env.get("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "").strip()
        self.limit = int(raw_limit) if raw_limit else 3
        self._secret = (
            settings.FORGE_LANE_CONTROL_SECRET.get_secret_value()
            if settings.FORGE_LANE_CONTROL_SECRET is not None
            else ""
        ) or app_env.get("FORGE_LANE_CONTROL_SECRET", "")
        self.notes: list[str] = []

    # -- native GitLab helpers ------------------------------------------

    def _note(self, issue_iid: int, body: str) -> None:
        response = gitlab_post(
            self.settings,
            f"/projects/{self.project_id}/issues/{issue_iid}/notes",
            json={"body": body},
        )
        if response.status_code not in (200, 201):
            raise Refused(f"note on issue {issue_iid} failed: {response.text[:200]}")

    def _notes(self, issue_iid: int) -> list[dict[str, Any]]:
        response = gitlab_get(
            self.settings, f"/projects/{self.project_id}/issues/{issue_iid}/notes"
        )
        response.raise_for_status()
        return response.json()

    def _run_detail(self, run_id: str) -> dict[str, Any]:
        response = httpx.get(f"{APP_API}/runs/{run_id}", timeout=30.0)
        response.raise_for_status()
        return response.json()

    def _pipeline_for(self, run_id: str, issue_iid: int) -> dict[str, Any] | None:
        branch = f"factory/{issue_iid}/{run_id[:8]}"
        response = gitlab_get(
            self.settings,
            f"/projects/{self.project_id}/pipelines",
            params={"ref": branch},
        )
        response.raise_for_status()
        pipelines = response.json()
        return pipelines[0] if pipelines else None

    def _lane_job(self, pipeline_id: int) -> dict[str, Any] | None:
        response = gitlab_get(
            self.settings, f"/projects/{self.project_id}/pipelines/{pipeline_id}/jobs"
        )
        response.raise_for_status()
        for job in response.json():
            if str(job.get("name", "")).startswith("forge-agent"):
                return job
        return None

    def _dispatch_pipeline_and_job(
        self, run_id: str, issue_iid: int
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The run's DISPATCH pipeline — the one carrying the forge-agent
        lane job — plus that job.

        A run's branch can carry SEVERAL pipelines (the dispatch pipeline
        with ``FORGE_RUN_ID``, then candidate/verification pipelines); the
        occupancy window belongs to the one holding the native lane job.
        """

        branch = f"factory/{issue_iid}/{run_id[:8]}"
        response = gitlab_get(
            self.settings, f"/projects/{self.project_id}/pipelines", params={"ref": branch}
        )
        response.raise_for_status()
        for pipeline in response.json():
            job = self._lane_job(int(pipeline.get("id") or 0))
            if job is not None:
                return pipeline, job
        return None

    def _lease_row(self, run_id: str) -> dict[str, str] | None:
        rows = psql_select(
            "SELECT run_id, slot, acquired_at, released_at, draining_at, "
            "native_intent_at, native_handle FROM execution_leases "
            f"WHERE run_id = '{run_id}' ORDER BY acquired_at DESC LIMIT 1"
        )
        return rows[0] if rows else None

    # -- the RemoteDispatchLane seam ------------------------------------

    async def occupancy_snapshot(self) -> dict[str, int]:
        rows = psql_select(
            "SELECT native_intent_at, native_handle, draining_at, released_at "
            "FROM execution_leases WHERE project_id = "
            f"{self.project_id} AND released_at IS NULL"
        )
        counts: dict[str, int] = {}
        for row in rows:
            if row["col3"]:
                word = "draining"
            elif not row["col0"]:
                word = "never_dispatched"
            elif row["col1"]:
                word = "native_running"
            else:
                word = "dispatched_unknown"
            counts[word] = counts.get(word, 0) + 1
        return counts

    async def lease_state(self, run_id: str) -> str:
        # Column order of _lease_row: run_id, slot, acquired_at,
        # released_at, draining_at, native_intent_at, native_handle.
        row = self._lease_row(run_id)
        if row is None or row["col3"]:
            return ""
        if row["col4"]:
            return "draining"
        if not row["col5"]:
            return "never_dispatched"
        return "native_running" if row["col6"] else "dispatched_unknown"

    async def dispatch(self, index: int) -> RemoteCycleRecord:
        record = RemoteCycleRecord(index=index)
        issue_iid = self.issues[index]
        # /implement → the app plans (one cheap model call per cycle).
        self._note(issue_iid, "@forge /implement")
        plan_run_id = ""
        deadline = time.monotonic() + 420.0
        while time.monotonic() < deadline:
            for note in reversed(self._notes(issue_iid)):
                body = str(note.get("body", ""))
                if note.get("author", {}).get("username") == "forge" and "/go " in body:
                    match = RUN_ID_RE.search(body)
                    if match is not None:
                        plan_run_id = match.group(1)
                        break
            if plan_run_id:
                break
            await asyncio.sleep(10.0)
        if not plan_run_id:
            record.end_state = "no_plan"
            record.detail = "the plan note never arrived within the bounded wait"
            return record
        run_id = plan_run_id
        self.runs[index] = run_id
        record.run_id = run_id

        # /go — the dispatch entry: the app reserves the execution lease
        # (the observed limit) and dispatches the REAL pipeline.
        requested_at = datetime.now(UTC)
        self._note(issue_iid, f"@forge /go {run_id}")
        record.end_state = "go_posted"
        dispatched: tuple[dict[str, Any], dict[str, Any]] | None = None
        blocked_reason = ""
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            dispatched = self._dispatch_pipeline_and_job(run_id, issue_iid)
            if dispatched is not None:
                break
            try:
                detail = self._run_detail(run_id)
                status = str(detail.get("status") or "")
                blocked_reason = str(detail.get("status_reason") or "")
                if status == "blocked" and blocked_reason:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(3.0)
        if dispatched is None:
            if blocked_reason:
                parked_at = datetime.now(UTC)
                record.end_state = "parked_" + blocked_reason.split(":")[0].strip(" -")
                record.detail = blocked_reason[:200]
                record.queue_wait_s = (parked_at - requested_at).total_seconds()
                return record
            record.end_state = "no_dispatch_verdict"
            record.detail = "neither a dispatch pipeline nor a blocked verdict within the wait"
            return record

        pipeline, job = dispatched
        record.pipeline_id = int(pipeline.get("id") or 0)
        record.job_id = int(job.get("id") or 0)
        lease = self._lease_row(run_id)
        acquired_at = datetime.fromisoformat(lease["col2"]) if lease and lease["col2"] else None
        record.lease_acquired = lease is not None and not lease["col3"]
        if acquired_at is not None and acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=UTC)
        record.queue_wait_s = (
            (acquired_at - requested_at).total_seconds() if acquired_at is not None else None
        )
        job_seen_at = datetime.now(UTC)
        base = acquired_at or requested_at
        record.execution_s = (job_seen_at - base).total_seconds()
        record.end_state = "dispatched"
        record.detail = (
            f"pipeline {record.pipeline_id}, lane job {record.job_id} "
            f"({job.get('status')}) on runner 4 (unraid)"
        )
        if index == 0:
            # The slow-control-ACK leg happens INSIDE the slot's window:
            # pause the run, let the command sit received (the slow lane),
            # then ack the ladder through the app's real endpoints.
            self.ack_measurement = await self._pause_and_slow_ack(run_id, issue_iid)
        return record

    async def _pause_and_slow_ack(self, run_id: str, issue_iid: int) -> dict[str, Any]:
        measurement: dict[str, Any] = {"work_id": run_id, "exercised": False}
        try:
            self._note(issue_iid, "@forge /pause")
            command: dict[str, Any] | None = None
            deadline = time.monotonic() + 180.0
            while time.monotonic() < deadline:
                controls = self._lane_controls(run_id)
                if controls:
                    command = controls[0]
                    break
                await asyncio.sleep(3.0)
            if command is None:
                measurement["note"] = "no control command arrived within the bounded wait"
                self.notes.append("slow-ack: no command observed")
                return measurement
            command_id = str(command.get("command_id"))
            await asyncio.sleep(self.slow_ack_delay_s)  # the SLOW lane
            ladder: list[tuple[str, dict[str, Any]]] = [
                ("authorized", {}),
                (
                    "dispatching",
                    {
                        "plan_revision": int(command.get("expected_plan_revision") or 0),
                        "execution_epoch": int(command.get("expected_execution_epoch") or 0),
                    },
                ),
                ("vendor_accepted", {}),
                ("applied", {}),
            ]
            statuses: list[int] = []
            for state, extra in ladder:
                response = httpx.post(
                    f"{APP_API}/lane/controls/{command_id}/ack",
                    headers=self._lane_headers(run_id),
                    json={"state": state, "generation": 0, **extra},
                    timeout=30.0,
                )
                statuses.append(response.status_code)
                if response.status_code not in (200, 409):
                    measurement["note"] = (
                        f"ack {state} answered {response.status_code}: {response.text[:160]}"
                    )
                    break
            # The received→applied window from the durable row itself
            # (read-only): created_at IS the received stamp (the row is
            # born 'received'), applied_at the applied transition.
            rows = psql_select(
                "SELECT created_at, applied_at FROM control_commands "
                f"WHERE id = '{command_id}' LIMIT 1"
            )
            received_at = rows[0]["col0"] if rows else ""
            applied_at = rows[0]["col1"] if rows else ""
            measurement.update(
                {
                    "exercised": bool(applied_at),
                    "command_id": command_id,
                    "ack_statuses": statuses,
                    "received_at": received_at,
                    "applied_at": applied_at,
                    "slow_ack_delay_s": self.slow_ack_delay_s,
                }
            )
            if received_at and applied_at:
                started = datetime.fromisoformat(received_at)
                finished = datetime.fromisoformat(applied_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                if finished.tzinfo is None:
                    finished = finished.replace(tzinfo=UTC)
                measurement["received_to_applied_s"] = round(
                    (finished - started).total_seconds(), 3
                )
            self.notes.append(
                f"slow-ack: {measurement.get('received_to_applied_s')}s received→applied"
            )
            return measurement
        except Exception as exc:  # noqa: BLE001 — recorded honestly, never retried green
            measurement["note"] = f"{type(exc).__name__}: {exc}"[:200]
            return measurement

    def _lane_headers(self, run_id: str) -> dict[str, str]:
        generation = 0
        token = lane_control_token(self._secret, run_id, generation=generation)
        return {"Authorization": f"Bearer {token}"}

    def _lane_controls(self, run_id: str) -> list[dict[str, Any]]:
        response = httpx.get(
            f"{APP_API}/lane/controls",
            params={"work_id": run_id},
            headers=self._lane_headers(run_id),
            timeout=30.0,
        )
        if response.status_code != 200:
            return []
        document = response.json()
        commands = document.get("commands") if isinstance(document, dict) else document
        return list(commands or [])

    async def cancel_immediately(self, record: RemoteCycleRecord) -> str:
        verdict = await self._cancel_job(record, drop_response=False)
        # Bounded spend: the run's branch may carry a retry pipeline the
        # reconciler opened after a fast failure — every non-terminal
        # pipeline on the branch is cancelled too (the observed lane job
        # above is the drill's job-level leg; this is the backstop).
        self._cancel_branch_pipelines(record.run_id, self.issues[record.index])
        return verdict

    async def cancel_with_lost_response(self, record: RemoteCycleRecord) -> None:
        # The client seam: the cancel IS issued, its response is dropped
        # without processing — occupancy resolves by the reconciler's own
        # observation, never by this answer.
        await self._cancel_job(record, drop_response=True)
        self._cancel_branch_pipelines(record.run_id, self.issues[record.index])

    def _cancel_branch_pipelines(self, run_id: str, issue_iid: int) -> None:
        branch = f"factory/{issue_iid}/{run_id[:8]}"
        try:
            response = gitlab_get(
                self.settings, f"/projects/{self.project_id}/pipelines", params={"ref": branch}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return
        for pipeline in response.json():
            if str(pipeline.get("status")) in {"running", "pending", "created", "preparing"}:
                gitlab_post(
                    self.settings,
                    f"/projects/{self.project_id}/pipelines/{pipeline['id']}/cancel",
                )

    async def _cancel_job(self, record: RemoteCycleRecord, *, drop_response: bool) -> str:
        if not record.job_id:
            return "no-job"
        try:
            if drop_response:
                # A 1s read timeout: the provider almost certainly applied
                # the cancel; we deliberately never process the answer.
                httpx.post(
                    f"{self.settings.GITLAB_URL.rstrip('/')}/api/v4/projects/"
                    f"{self.project_id}/jobs/{record.job_id}/cancel",
                    headers={"PRIVATE-TOKEN": self.settings.GITLAB_TOKEN.get_secret_value()},
                    timeout=1.0,
                )
                return "dropped"
            response = gitlab_post(
                self.settings, f"/projects/{self.project_id}/jobs/{record.job_id}/cancel"
            )
            return (
                f"ok:{response.status_code}"
                if response.status_code < 300
                else (f"failed:{response.status_code}")
            )
        except httpx.HTTPError as exc:
            return f"dropped ({type(exc).__name__})" if drop_response else f"failed:{exc}"[:80]

    async def cancel_completed_job(self, record: RemoteCycleRecord) -> dict[str, Any]:
        """A job-level cancel of an ALREADY-completed job (the seed
        pipeline's smoke job). The provider either refuses the cancel or
        answers an idempotent no-op — either way the job's state must not
        change and no capacity may move."""

        result: dict[str, Any] = {
            "exercised": False,
            "verdict": "",
            "status_before": "",
            "status_after": "",
        }
        response = gitlab_get(
            self.settings, f"/projects/{self.project_id}/pipelines", params={"ref": "main"}
        )
        response.raise_for_status()
        pipelines = response.json()
        if not pipelines:
            return result
        jobs = gitlab_get(
            self.settings, f"/projects/{self.project_id}/pipelines/{pipelines[0]['id']}/jobs"
        )
        jobs.raise_for_status()
        completed = next(
            (job for job in jobs.json() if str(job.get("status")) in {"success", "failed"}),
            None,
        )
        if completed is None:
            return result
        status_before = str(completed.get("status"))
        cancel = gitlab_post(
            self.settings, f"/projects/{self.project_id}/jobs/{completed['id']}/cancel"
        )
        verdict = (
            f"answered:{cancel.status_code}"
            if cancel.status_code < 300
            else (f"refused:{cancel.status_code}")
        )
        after = gitlab_get(self.settings, f"/projects/{self.project_id}/jobs/{completed['id']}")
        status_after = str(after.json().get("status")) if after.status_code == 200 else ""
        result.update(
            {
                "exercised": True,
                "verdict": verdict,
                "status_before": status_before,
                "status_after": status_after,
            }
        )
        self.notes.append(
            f"failed-cancel leg: job {completed['id']} ({status_before}) → {verdict} "
            f"(job still {status_after})"
        )
        return result

    async def wait_project_drained(self, timeout_s: float) -> tuple[bool, float]:
        started = time.monotonic()
        while time.monotonic() - started < timeout_s:
            rows = psql_select(
                "SELECT run_id FROM execution_leases WHERE project_id = "
                f"{self.project_id} AND released_at IS NULL"
            )
            if not rows:
                return True, time.monotonic() - started
            await asyncio.sleep(10.0)
        rows = psql_select(
            "SELECT run_id FROM execution_leases WHERE project_id = "
            f"{self.project_id} AND released_at IS NULL"
        )
        return (not rows), time.monotonic() - started


def setup_disposable_project(settings: Settings, name: str) -> int:
    """Create + seed the DISPOSABLE GitLab project (the R37-08 shape):
    pinned lane template, copied lane credentials, the forge webhook, the
    bot member, and a tiny smoke job whose completion feeds the
    failed-cancel leg."""

    who = gitlab_get(settings, "/user")
    who.raise_for_status()
    created = gitlab_post(
        settings,
        "/projects",
        json={
            "name": name,
            "path": name,
            "namespace_id": who.json().get("namespace_id"),
            "visibility": "private",
            "initialize_with_readme": False,
            "builds_access_level": "enabled",
        },
    )
    if created.status_code not in (200, 201):
        raise Refused(f"disposable project creation failed: {created.text[:200]}")
    project_id = int(created.json()["id"])
    log(f"occupancy: disposable project {created.json()['path_with_namespace']} (id {project_id})")

    ci_yaml = (
        f"include:\n"
        f"  - remote: '{TEMPLATE_URL}'\n"
        f"\n"
        # The v0.36.0 template MANUAL RESCUE (live-found in the R37-08
        # qualification): the SDK lane template exits a SUCCESSFUL driver
        # run before .forge/candidate.diff is built, which parks every run
        # blocked(harness_artifact_missing) and auto-re-drives it — exactly
        # the unbounded retry the occupancy drill must not trigger. The
        # same one-line guarded-exit patch the live qualification seeded.
        f"{_patched_lane_override()}\n"
        f"stages: [test, harness]\n"
        f"\n"
        f"# A deliberately tiny always-green job: its COMPLETION is the\n"
        f"# deployment drill's failed-cancel leg (a completed job cannot\n"
        f"# be cancelled — the provider's refusal, capacity unmoved).\n"
        f"smoke:\n"
        f"  stage: test\n"
        f"  image: python:3.13-slim\n"
        f"  rules:\n"
        f"    - if: '$FORGE_RUN_ID'\n"
        f"      when: never\n"
        f"    - when: on_success\n"
        f"  script:\n"
        f"    - python -c \"print('smoke ok')\"\n"
    )
    commit = gitlab_post(
        settings,
        f"/projects/{project_id}/repository/commits",
        json={
            "branch": "main",
            "commit_message": "seed: deployment-ops disposable project (R37-20)",
            "actions": [
                {"action": "create", "file_path": "README.md", "content": f"# {name}\n"},
                {"action": "create", "file_path": ".gitlab-ci.yml", "content": ci_yaml},
            ],
        },
    )
    if commit.status_code not in (200, 201):
        raise Refused(f"seed commit failed: {commit.text[:200]}")

    copied: list[str] = []
    for key in VARIABLES_FROM_LAB:
        entry = gitlab_get(settings, f"/projects/{LAB_PROJECT_ID}/variables/{key}")
        if entry.status_code != 200:
            continue
        value = str(entry.json().get("value") or "")
        if not value:
            continue
        response = gitlab_post(
            settings, f"/projects/{project_id}/variables", json={"key": key, "value": value}
        )
        if response.status_code not in (200, 201):
            raise Refused(f"variable {key} copy failed: {response.text[:200]}")
        copied.append(key)
    for key, value in (
        ("FORGE_LANE_REF", LANE_REF_SHA),
        ("FORGE_STEERING_ENABLED", "1"),
    ):
        response = gitlab_post(
            settings, f"/projects/{project_id}/variables", json={"key": key, "value": value}
        )
        if response.status_code not in (200, 201):
            raise Refused(f"variable {key} set failed: {response.text[:200]}")
        copied.append(key)

    hooks = gitlab_get(settings, f"/projects/{LAB_PROJECT_ID}/hooks")
    hooks.raise_for_status()
    forge_hook = next(
        (hook for hook in hooks.json() if "/webhook" in str(hook.get("url", ""))), None
    )
    if forge_hook is None:
        raise Refused("the lab project carries no forge webhook to replicate")
    hook = gitlab_post(
        settings,
        f"/projects/{project_id}/hooks",
        json={
            "url": forge_hook["url"],
            "token": settings.GITLAB_WEBHOOK_SECRET.get_secret_value(),
            "push_events": True,
            "merge_requests_events": True,
            "note_events": True,
            "pipeline_events": True,
            "job_events": True,
            "issues_events": True,
            "enable_ssl_verification": False,
        },
    )
    if hook.status_code not in (200, 201):
        raise Refused(f"webhook registration failed: {hook.text[:200]}")

    bot_token = (
        settings.FORGE_BOT_TOKEN.get_secret_value() if settings.FORGE_BOT_TOKEN is not None else ""
    )
    if bot_token:
        with httpx.Client(
            base_url=f"{settings.GITLAB_URL.rstrip('/')}/api/v4",
            headers={"PRIVATE-TOKEN": bot_token},
            timeout=30.0,
        ) as bot_client:
            bot = bot_client.get("/user")
            bot.raise_for_status()
            member = gitlab_post(
                settings,
                f"/projects/{project_id}/members",
                json={"user_id": bot.json()["id"], "access_level": 30},
            )
            if member.status_code not in (200, 201):
                raise Refused(f"bot membership grant failed: {member.text[:200]}")
    log(f"occupancy: seeded (variables {copied}; webhook {forge_hook['url']})")
    return project_id


async def section_occupancy(settings: Settings, project_id: int, *, cycles: int) -> dict[str, Any]:
    lane = LabRemoteDispatchLane(settings, project_id, cycles=cycles)
    for index in range(cycles):
        created = gitlab_post(
            settings,
            f"/projects/{project_id}/issues",
            json={
                "title": f"ops-drill task {index + 1}: append one line",
                "description": TASK_BODY,
            },
        )
        if created.status_code not in (200, 201):
            raise Refused(f"issue creation failed: {created.text[:200]}")
        lane.issues.append(int(created.json()["iid"]))
    log(f"occupancy: {cycles} issues created — driving the app's own dispatch entry")
    outcome = await drill_remote_occupancy(
        lane, cycles=cycles, reconcile_timeout_s=300.0, sample_interval_s=3.0
    )
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    document["lane_notes"] = lane.notes
    document["slow_control_ack"] = lane.ack_measurement
    # The parked cycle's issue is closed so nothing re-drives it later.
    for index, run_id in lane.runs.items():
        try:
            detail = lane._run_detail(run_id)  # noqa: SLF001 — this driver owns the lane
            if str(detail.get("state")) == "blocked":
                gitlab_put(
                    settings,
                    f"/projects/{project_id}/issues/{lane.issues[index]}",
                    json={"state_event": "close"},
                )
        except (httpx.HTTPError, Refused):
            pass
    return document


# ---------------------------------------------------------------------------
# Section 3 — backup/restore across the real deployment
# ---------------------------------------------------------------------------


def pg_dump_checkpoint_metadata(destination: Path) -> dict[str, Any]:
    """READ-ONLY pg_dump of the checkpoint metadata out of the lab pg."""

    inner = "/tmp/deployment-ops-checkpoint-metadata.dump"
    dump = subprocess.run(
        [
            "podman",
            "exec",
            "forge-postgres",
            "pg_dump",
            "-U",
            "forge",
            "-d",
            "forge",
            "-t",
            "public.checkpoint_metadata",
            "-Fc",
            "-f",
            inner,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if dump.returncode != 0:
        raise Refused(f"pg_dump failed: {dump.stderr.strip()[:200]}")
    copied = subprocess.run(
        ["podman", "cp", f"forge-postgres:{inner}", str(destination)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if copied.returncode != 0:
        raise Refused(f"pg_dump copy-out failed: {copied.stderr.strip()[:200]}")
    subprocess.run(
        ["podman", "exec", "forge-postgres", "rm", "-f", inner],
        capture_output=True,
        timeout=30,
        check=False,
    )
    blob = destination.read_bytes()
    return {
        "path": str(destination.name),
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "note": "read-only pg_dump of the checkpoint metadata (no container stop)",
    }


def psql_admin(statement: str) -> subprocess.CompletedProcess:
    """A DISPOSABLE-database administration statement (never 'forge')."""

    if not re.fullmatch(
        rf"(CREATE|DROP) DATABASE( IF EXISTS)? {RESTORE_DB}( WITH \(FORCE\))?", statement.strip()
    ):
        raise Refused(
            f"psql_admin refuses a statement outside the disposable database: {statement!r}"
        )
    return subprocess.run(
        [
            "podman",
            "exec",
            "forge-postgres",
            "psql",
            "-U",
            "forge",
            "-d",
            "forge",
            "-c",
            statement,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def section_backup_restore(work_dir: Path) -> dict[str, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from forge.models.base import Base

    if not STORE_ROOT.is_dir():
        raise Refused(f"the deployment CAS root {STORE_ROOT} does not exist on this host")

    # The metadata half: read-only SELECTs through a connection to the
    # REAL database (backup_store only reads), plus a read-only pg_dump.
    source_url = "postgresql+asyncpg://forge:forge@127.0.0.1:5433/forge"
    source_engine = create_async_engine(source_url, poolclass=NullPool)
    source_factory = async_sessionmaker(source_engine, expire_on_commit=False)
    metadata_rows = 0
    try:
        async with source_factory() as session:
            from sqlalchemy import text

            metadata_rows = int(
                (await session.execute(text("SELECT count(*) FROM checkpoint_metadata"))).scalar()
                or 0
            )
        outcome = await drill_restore_deployment(
            STORE_ROOT,
            work_dir=work_dir,
            source_session_factory=source_factory,
            restore_session_factory=None,
        )
    finally:
        await source_engine.dispose()

    # The disposable DATABASE restore target: the postgres authority's
    # metadata rows re-imported into forge_ops_restore (created + dropped).
    engine: Any = None
    db_restore: dict[str, Any] = {"database": RESTORE_DB, "exercised": False}
    try:
        created = psql_admin(f"CREATE DATABASE {RESTORE_DB}")
        if created.returncode != 0:
            raise Refused(f"disposable database creation failed: {created.stderr[:200]}")
        restore_url = f"postgresql+asyncpg://forge:forge@127.0.0.1:5433/{RESTORE_DB}"
        engine = create_async_engine(restore_url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        restore_factory = async_sessionmaker(engine, expire_on_commit=False)
        from forge.adaptive.checkpoint_repository import (
            PostgresCheckpointRepository,
            backup_store,
            restore_store,
        )

        async with source_factory() as session:
            from sqlalchemy import text

            source_rows = int(
                (await session.execute(text("SELECT count(*) FROM checkpoint_metadata"))).scalar()
                or 0
            )
        backup = await backup_store(
            STORE_ROOT, work_dir / "db-backup", session_factory=source_factory
        )
        target = work_dir / "db-restore-target"
        from forge.adaptive.checkpoint_repository import BackupMismatchError

        try:
            coverage = await restore_store(backup, target, session_factory=restore_factory)
        except BackupMismatchError as exc:
            # A REAL finding about the deployment's data boundary, not a
            # driver failure: the exported checkpoint_metadata names bytes
            # the CAS blob half does not carry (a stale row whose bytes
            # are gone) — the verify-before-restore contract refuses
            # typed, which is exactly the detection the report records.
            db_restore.update(
                {
                    "exercised": True,
                    "mismatch_refused": True,
                    "mismatch_affected_works": sorted(
                        {str(item.get("work_id")) for item in exc.affected}
                    ),
                    "finding": (
                        "the deployment's checkpoint_metadata carries row(s) whose "
                        "CAS bytes are absent — a backup of BOTH halves refuses "
                        "typed on restore (detect-missing-bytes observed on the "
                        "real data; the row predates this deployment's live work)"
                    ),
                }
            )
        else:
            restored = PostgresCheckpointRepository(target, restore_factory)
            # Resolve the works the EXPORTED METADATA names (the postgres
            # authority's own index — not the filesystem overlay).
            resolved = 0
            metadata_works: list[str] = []
            rows_file = backup.path / "checkpoint_metadata.jsonl"
            if rows_file.is_file():
                for line in rows_file.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        metadata_works.append(str(row.get("work_id")))
            for work_id in sorted(set(metadata_works)):
                entry = await restored.entry(work_id)
                if entry is not None and entry.get("checkpoint_id"):
                    manifest, _blobs = await restored.read_entry(entry)
                    if manifest is not None:
                        resolved += 1
            db_restore.update(
                {
                    "exercised": True,
                    "mismatch_refused": False,
                    "metadata_rows_source": source_rows,
                    "metadata_rows_restored": int(coverage.get("metadata_rows") or 0),
                    "metadata_works_resolved_via_postgres_authority": resolved,
                }
            )
    except Refused as exc:
        db_restore["refused"] = str(exc)[:200]
    finally:
        if engine is not None:
            await engine.dispose()
        psql_admin(f"DROP DATABASE IF EXISTS {RESTORE_DB} WITH (FORCE)")

    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    document["pg_dump"] = pg_dump_checkpoint_metadata(work_dir / "checkpoint-metadata.dump")
    document["metadata_rows_read_only_count"] = metadata_rows
    document["disposable_database_restore"] = db_restore
    signals = document["signals"]["checkpoint.reachability"]
    signals["pg_dump_captured"] = True
    log(
        f"backup_restore: {signals['works_resolved']}/{signals['works']} works, "
        f"{signals['pins_resolved']}/{signals['pins']} pins, mismatch refused "
        f"{signals['mismatch_refused']}, metadata rows {metadata_rows}"
    )
    return document


# ---------------------------------------------------------------------------
# Section 4 — credential isolation + egress denial
# ---------------------------------------------------------------------------


def _deny_probe(name: str, url: str, header: str, value: str) -> dict[str, Any]:
    """The #296 executor probe pattern against a LIVE surface: present a
    credential the surface must refuse, record the verdict (never the
    value)."""

    try:
        response = httpx.get(url, headers={header: value}, timeout=15.0)
    except httpx.HTTPError as exc:
        return {
            "name": name,
            "url": url,
            "outcome": "denied-refused",
            "status_code": None,
            "detail": f"{type(exc).__name__}",
        }
    if response.status_code // 100 == 2:
        outcome, detail = "violated", "the surface served this credential"
    elif response.status_code in (401, 403, 407):
        outcome, detail = "denied-unauthorized", f"refused ({response.status_code})"
    else:
        outcome, detail = "denied-other-status", f"answered {response.status_code}"
    return {
        "name": name,
        "url": url,
        "outcome": outcome,
        "status_code": response.status_code,
        "detail": detail,
    }


def section_credentials(settings: Settings) -> dict[str, Any]:
    if not LIVE_EVIDENCE.is_file():
        raise Refused(f"the live-run evidence is absent: {LIVE_EVIDENCE}")
    evidence = json.loads(LIVE_EVIDENCE.read_text(encoding="utf-8"))
    evidence_full = (
        evidence.get("phases", {})
        .get("collect-flow", {})
        .get("durable_state", {})
        .get("evidence_full", {})
    )
    envelope = (evidence_full.get("harness") or {}).get("dispatch_envelope") or {}
    if not envelope:
        raise Refused("the live-run evidence carries no dispatch envelope")

    app_env = inspect_container("forge-app")["env"]
    lane_url = f"{APP_API}/lane/controls"
    # The presented VALUE never enters the report — only its label.
    presented: list[tuple[str, str]] = []
    if settings.FORGE_MCP_KEY is not None:
        presented.append(
            ("the MCP master key (FORGE_MCP_KEY)", settings.FORGE_MCP_KEY.get_secret_value())
        )
    if app_env.get("ZAI_API_KEY"):
        presented.append(("the root model broker key (ZAI_API_KEY)", app_env["ZAI_API_KEY"]))
    presented.append(
        ("the GitLab root token (GITLAB_TOKEN)", settings.GITLAB_TOKEN.get_secret_value())
    )
    probes: list[dict[str, Any]] = []
    for label, value in presented:
        probe = _deny_probe(
            f"lane-control with {label}", lane_url, "Authorization", f"Bearer {value}"
        )
        probe["presented"] = label
        probes.append(probe)
    # The lane credentials must NOT open the operator surface either —
    # the isolation boundary cuts both ways.
    lane_secret = (
        settings.FORGE_LANE_CONTROL_SECRET.get_secret_value()
        if settings.FORGE_LANE_CONTROL_SECRET is not None
        else ""
    )
    if lane_secret:
        a_lane_token = lane_control_token(lane_secret, "deployment-ops-probe", generation=0)
        probe = _deny_probe(
            "operator surface with a lane token",
            f"{APP_API}/operator/runs",
            "Authorization",
            f"Bearer {a_lane_token}",
        )
        probe["presented"] = "a work-scoped lane token"
        probes.append(probe)

    outcome = drill_credential_isolation(
        envelope=envelope,
        control_plane_env={name: "" for name in app_env},  # names only — never values
        deny_probes=probes,
    )
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    document["envelope_source"] = str(LIVE_EVIDENCE.relative_to(REPO_ROOT))
    denied = sum(1 for probe in probes if str(probe.get("outcome", "")).startswith("denied-"))
    log(
        f"credentials: {len(envelope.get('variable_keys') or [])} model-facing variables, "
        f"{denied}/{len(probes)} deny probes denied"
    )
    return document


# ---------------------------------------------------------------------------
# Section 5 — degraded modes through the app's real endpoints
# ---------------------------------------------------------------------------


async def section_degraded(
    settings: Settings, work_dir: Path, ack_measurement: dict[str, Any]
) -> dict[str, Any]:
    async def health_ok() -> bool:
        try:
            return app_health().get("status") == "ok"
        except httpx.HTTPError:
            return False

    async def control_ack_cycle() -> dict[str, Any]:
        return ack_measurement

    revive_limit = 2
    raw = str(inspect_container("forge-app")["env"].get("FORGE_RUN_AUTO_REVIVE_LIMIT", "")).strip()
    if raw:
        revive_limit = int(raw)
    outcome = await drill_degraded_modes(
        work_dir=work_dir,
        health_ok=health_ok,
        control_ack_cycle=control_ack_cycle,
        revive_limit=revive_limit,
        control_objective_s=60.0,
    )
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    log(f"degraded: {document['outcome']} ({len(document['violations'])} violation(s))")
    return document


# ---------------------------------------------------------------------------
# Section 6 — token rotation on a disposable configuration
# ---------------------------------------------------------------------------


async def section_rotation(settings: Settings, work_dir: Path) -> dict[str, Any]:
    current = (
        settings.FORGE_LANE_CONTROL_SECRET.get_secret_value()
        if settings.FORGE_LANE_CONTROL_SECRET is not None
        else ""
    )
    if not current:
        # The host .env may not carry the lane secret; the DEPLOYED app
        # does (read-only inspect — the value never enters the report).
        current = inspect_container("forge-app")["env"].get("FORGE_LANE_CONTROL_SECRET", "")
    if not current:
        raise Refused("FORGE_LANE_CONTROL_SECRET is configured nowhere this run can read")
    # The deployment's env is NOT touched: the rotation happens on a
    # DISPOSABLE configuration (a fresh v2 secret + a disposable run id).
    rotated = "rotated-" + pysecrets.token_hex(16)
    work_id = "opsrotation" + uuid_mod.uuid4().hex[:10]
    outcome = await drill_token_rotation(
        secret_current=current,
        secret_next=rotated,
        work_id=work_id,
        work_dir=work_dir / "rotation",
    )
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    signal = document["signals"]["credential.rotation_generation"]
    log(
        f"rotation: old-secret token {signal['old_secret_token_status']}, "
        f"old-generation token {signal['refusal_status']}, "
        f"v2 token {signal['new_secret_token_status']}"
    )
    return document


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

SECTIONS = ("topology", "occupancy", "backup_restore", "credentials", "degraded", "rotation")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_deployment_ops",
        description="R37-20: qualify the deployment's operating boundaries (read-only + disposable).",
    )
    parser.add_argument("--out", type=Path, default=None, help="write the JSON report to this path")
    parser.add_argument(
        "--sections",
        default="all",
        help=f"comma-separated sections (or 'all'): {', '.join(SECTIONS)}",
    )
    parser.add_argument("--cycles", type=int, default=4, help="dispatch cycles (default 4)")
    parser.add_argument(
        "--keep-project", action="store_true", help="keep the disposable GitLab project"
    )
    parser.add_argument("--keep-work", action="store_true", help="keep the disposable work dir")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    work_dir = Path(tempfile.mkdtemp(prefix="forge-deployment-ops-"))
    report: dict[str, Any] = {
        "schema": REPORT_STAMP,
        "issue": "R37-20 (#301) — qualify customer operating limits, recovery and data boundaries",
        "generated_at": _now_iso(),
        "scope": DEPLOYMENT_DRILL_SCOPE,
        "read_only": True,
        "sections_requested": [],
        "topology": None,
        "drills": [],
        "refusals": [],
        "service_objectives": None,
        "runbook": "docs/operations/deployment-boundaries.md",
    }
    selected = (
        list(SECTIONS)
        if args.sections.strip().lower() == "all"
        else [name.strip() for name in args.sections.split(",") if name.strip()]
    )
    report["sections_requested"] = selected

    def record_drill(document: dict[str, Any]) -> None:
        report["drills"].append(document)
        for objective in document.get("achieved_objectives", []):
            log(f"   [ok] {objective}")
        for violation in document.get("violations", []):
            log(f"   [VIOLATION] {violation}")

    def record_refusal(section: str, exc: Refused | Exception) -> None:
        entry = {"section": section, "reason": str(exc)[:400]}
        report["refusals"].append(entry)
        log(f"   [REFUSED] {section}: {entry['reason']}")

    occupancy_document: dict[str, Any] | None = None
    ack_measurement: dict[str, Any] = {}
    project_id: int | None = None
    try:
        if "topology" in selected:
            log("== section: topology (read-only)")
            try:
                report["topology"] = section_topology(settings)
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("topology", exc)

        if "occupancy" in selected:
            log("== section: occupancy (disposable project, real native jobs, immediate cancels)")
            name = f"forge-ops-301-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            try:
                project_id = setup_disposable_project(settings, name)
                occupancy_document = await section_occupancy(
                    settings, project_id, cycles=args.cycles
                )
                record_drill(occupancy_document)
                ack_measurement = dict(occupancy_document.get("slow_control_ack") or {})
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("occupancy", exc)

        if "backup_restore" in selected:
            log("== section: backup_restore (read-only snapshot + disposable targets)")
            try:
                record_drill(await section_backup_restore(work_dir))
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("backup_restore", exc)

        if "credentials" in selected:
            log("== section: credentials (recorded envelope + live deny probes)")
            try:
                record_drill(section_credentials(settings))
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("credentials", exc)

        if "degraded" in selected:
            log("== section: degraded (quota tmp dir, 429 budget, slow control ACK)")
            try:
                record_drill(await section_degraded(settings, work_dir, ack_measurement))
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("degraded", exc)

        if "rotation" in selected:
            log("== section: rotation (disposable configuration, generation-scoped refusal)")
            try:
                record_drill(await section_rotation(settings, work_dir))
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("rotation", exc)

        # -- the agreed service objectives, with the measured values ----
        queue_wait_max = None
        received_to_applied = None
        restore_seconds = None
        for document in report["drills"]:
            if document.get("drill") == "deployment_remote_occupancy":
                queue_wait_max = (document.get("signals", {}).get("queue_wait_s") or {}).get("max")
            if document.get("drill") == "deployment_backup_restore":
                restore_seconds = document.get("signals", {}).get("restore_seconds")
        if ack_measurement:
            received_to_applied = ack_measurement.get("received_to_applied_s")
        degraded_document = next(
            (d for d in report["drills"] if d.get("drill") == "deployment_degraded_modes"), None
        )
        report["service_objectives"] = {
            "note": (
                "the small agreed-objectives table; every value is THIS run's "
                "measurement (the exact N/waits are in each drill's tested_limits)"
            ),
            "objectives": [
                {
                    "objective": "queue wait (request → lease), worst cycle",
                    "target": "bounded, reported separately from execution",
                    "measured_s": queue_wait_max,
                    "guarantee": "best-effort (webhook + dispatch latency)",
                },
                {
                    "objective": "control.received_to_applied (slow ACK tolerated)",
                    "target": "≤ 60s through the app's real lane-control endpoints",
                    "measured_s": received_to_applied,
                    "guarantee": "best-effort (the ladder is bounded by the mailbox CAS)",
                },
                {
                    "objective": "restore of the deployment's CAS + metadata into a disposable target",
                    "target": "every pinned checkpoint resolves; mismatch detected",
                    "measured_s": restore_seconds,
                    "guarantee": "guaranteed by verify-before-restore (typed refusal)",
                },
                {
                    "objective": "capacity: open leases ≤ the observed per-project bound",
                    "target": "never exceeded; overload parks typed",
                    "measured": (
                        (occupancy_document or {})
                        .get("signals", {})
                        .get("execution.occupied_vs_limit")
                    ),
                    "guarantee": "guaranteed (durable lease CAS; lost responses hold)",
                },
            ],
            "documented_degraded_modes": ((degraded_document or {}).get("signals") or {}),
        }
        report["summary"] = {
            "drills_run": len(report["drills"]),
            "passed": sum(1 for d in report["drills"] if d.get("outcome") == "pass"),
            "failed": sum(1 for d in report["drills"] if d.get("outcome") == "fail"),
            "refused_sections": len(report["refusals"]),
        }
    finally:
        if project_id is not None and not args.keep_project:
            try:
                response = gitlab_delete(settings, f"/projects/{project_id}")
                if response.status_code in (200, 202, 204):
                    log(f"teardown: disposable project {project_id} deleted")
                else:
                    log(f"teardown: project delete answered {response.status_code}")
            except httpx.HTTPError as exc:
                log(f"teardown: project delete failed: {exc}")
        if args.keep_work:
            log(f"work directory kept: {work_dir}")
        else:
            import shutil

            shutil.rmtree(work_dir, ignore_errors=True)
    return report


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.out is None:
        args.out = REPO_ROOT / "qualification" / "deployment-ops-2026-09-24.json"
    settings = Settings()
    report = asyncio.run(_run(args, settings))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"report: {args.out}")
    summary = report.get("summary", {})
    log(
        f"summary: {summary.get('passed', 0)}/{summary.get('drills_run', 0)} drills passed "
        f"({summary.get('failed', 0)} failed, {summary.get('refused_sections', 0)} section(s) refused)"
    )
    log(f"scope: {report['scope']}")
    failed = summary.get("failed", 0) > 0 or summary.get("refused_sections", 0) > 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
