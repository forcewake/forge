#!/usr/bin/env python3
"""R38-18 (issue #319) — measure the deployment's operating limits BOUND
to the frozen supported profile.

The R37-20 drills (#301) qualified the deployment as it stood. The
supported profile is now FROZEN
(``qualification/profiles/supported-gitlab-ce-v1.json``, #307 — manifest
digest 7c292dd8…, the executed-lab bind), so every measurement this
runner takes is evidence ONLY for the deployment that matches that
bind: the runner observes the deployment read-only (image identity,
schema head, reported version), records the manifest digest on EVERY
drill row, and renders ``unqualified-for-profile`` on any mismatch —
never a silent pass against a different deployment.

The R37-20 sections (green at the 2026-09-24 run) stay:

- ``topology``      — the deployment DECLARED from read-only probes.
- ``occupancy``     — N concurrent dispatch cycles through the app's OWN
  entry (issue → /implement → /go on a DISPOSABLE GitLab project): REAL
  native jobs, cancelled job-level immediately after the observation.
- ``backup_restore``— ``backup_store`` over the REAL CAS root (read-only)
  + restore into DISPOSABLE targets; mismatched halves refuse.
- ``credentials``   — the recorded dispatch envelope + live deny probes.
- ``degraded``      — typed quota refusal, the bounded 429 revival
  budget, the slow control ACK — fences never disabled.
- ``rotation``      — the lane-control secret rotated on a DISPOSABLE
  configuration.

The R38-18 arms (the review's named deployment-only failures, every one
profile-bound):

- ``cap_arm``           — a native dispatch response DROPPED at the
  client seam + the concurrency cap reached immediately: the dropped
  run's unknown occupancy keeps consuming capacity (proven from the
  durable lease row) until the reconciler resolves it by observation;
  the cycle after the cap parks typed (#241 semantics AT the cap).
- ``volume_fill``       — the checkpoint volume filled to the configured
  safety threshold while a run is PAUSED with a pinned checkpoint: the
  pinned WIP survives, new writes refuse typed, admission stops
  predictably (a quota-tmp-dir store shape — a real disk is never
  filled; the typed-refusal path is what is measured).
- ``mismatched_restore``— mismatched metadata/blob snapshots restored
  into DISPOSABLE installations: the preflight refuses BEFORE any new
  model turn (schema-head bind against the frozen profile's 027, and
  the halves consistency the backup contract already enforces).
- ``percentiles``       — pause/cancel responsiveness with STATED
  percentiles and scope under upload + slow-provider load, through the
  REAL lane-control endpoints over a disposable database.

The report also carries the measured-limits table (concurrency under
the controlled failures, pause/cancel percentiles, command latency,
restore time — the runbook's numbers, updated to the frozen profile)
and the reviewer-WIP bound (a STATED POLICY field: admission vs the
reviewable volume, never a throughput claim).

PUBLICATION (the #304 discipline): ``--out`` receives the SANITIZED
summary (``forge.deployment.ops.sanitized/1``); the full diagnostics
report (``forge.deployment.ops/1`` — identifiers, per-cycle details,
host paths) is written OUTSIDE the repository to a private directory
and only its retention is referenced. Raw operational diagnostics never
enter the public tree.

READ-ONLY + DISPOSABLE only: no lab container is started, stopped or
recreated; the only writes are (a) the DISPOSABLE GitLab project + its
issues/notes/pipelines (deleted at teardown) and (b) DISPOSABLE local
temp roots + a disposable database (dropped at teardown). Native jobs
are cancelled immediately after the observation (bounded spend).

Usage:

    uv run python scripts/run_deployment_ops.py --out qualification/deployment-ops-2026-09-25.json

Exit codes: 0 every executed drill passed and the profile bind matched ·
1 a drill failed, a section refused, the profile bind mismatched or the
reviewer-WIP policy is incoherent (the report is still written — a
refusal is evidence).
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
    PROFILE_QUALIFIED,
    CapBoundaryLane,
    RemoteCycleRecord,
    WorkflowCycleRecord,
    WorkflowShapeLane,
    build_fixture,
    build_topology_document,
    drill_credential_isolation,
    drill_degraded_modes,
    drill_degradation_parking,
    drill_lost_response_at_cap,
    drill_mismatched_restore_preflight,
    drill_pause_cancel_percentiles,
    drill_partition_occupancy,
    drill_redemption_lane,
    drill_remote_occupancy,
    drill_restore_deployment,
    drill_token_rotation,
    drill_volume_fill_during_pause,
    drill_workflow_envelope,
    drill_workflow_restore,
    profile_binding_row,
    reviewer_wip_bound_row,
    summarize_for_publication,
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

#: The FROZEN supported profile (#307) every R38-18 measurement binds to.
SUPPORTED_PROFILE = REPO_ROOT / "qualification" / "profiles" / "supported-gitlab-ce-v1.json"

#: Where the FULL diagnostics report (the pre-sanitization document)
#: lands: OUTSIDE the repository, per the #304 discipline — the public
#: tree carries the sanitized summary only.
PRIVATE_REPORT_DIR = Path.home() / ".forge-private" / "deployment-ops"

#: The configured safety threshold for the volume-fill arm's quota-tmp-dir
#: store (bytes; a real disk is never filled — the typed-refusal path is
#: what is measured).
VOLUME_FILL_SAFETY_THRESHOLD_BYTES = 4096

#: The stated reviewer-WIP policy bound (R38-18 human-capacity
#: discipline): how much concurrent WIP one human reviewer can safely
#: review. A POLICY FIELD, never a measurement — overridable per run.
DEFAULT_REVIEWER_WIP_BOUND = 5


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


def observe_deployment_bind() -> dict[str, str]:
    """The READ-ONLY deployment observation the profile bind compares.

    Every axis the frozen profile's ``control_plane.executed_lab``
    names: the forge-app image identity (name, id, digest — read-only
    ``podman inspect``), the deployed schema head (read-only SELECT
    against ``alembic_version``) and the reported version (``/health``).
    Nothing here writes a lab row or touches a container lifecycle.
    """

    entry = podman_json("inspect", "forge-app")
    raw = entry[0] if isinstance(entry, list) and entry else {}
    image_name = str(raw.get("ImageName") or raw.get("Config", {}).get("Image") or "")
    image_id = str(raw.get("Image") or "")
    image_digest = ""
    if image_id:
        image = podman_json("image", "inspect", image_id)
        image_digest = (
            str((image[0] or {}).get("Digest") or "") if isinstance(image, list) and image else ""
        )
    schema_rows = psql_select("SELECT version_num FROM alembic_version")
    return {
        "image_name": image_name,
        "image_id": image_id,
        "image_digest": image_digest,
        "schema_head": schema_rows[0]["col0"] if schema_rows else "",
        "reported_version": str(app_health().get("version") or ""),
    }


def load_profile_binding() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the FROZEN supported profile and compute the bind row.

    Returns ``(manifest_document, binding_row)``. A missing or unreadable
    manifest refuses loudly (the caller records the refusal) — the
    measurements never run unbound.
    """

    if not SUPPORTED_PROFILE.is_file():
        raise Refused(f"the frozen supported profile is absent: {SUPPORTED_PROFILE}")
    manifest = json.loads(SUPPORTED_PROFILE.read_text(encoding="utf-8"))
    binding = profile_binding_row(manifest, observe_deployment_bind())
    log(
        "profile: "
        f"{binding['profile']} (manifest {str(binding['manifest_digest'])[:16]}…) — "
        f"bind {binding['bind']} → {binding['qualification']}"
    )
    for difference in binding.get("differences") or []:
        log(f"   [profile difference] {difference}")
    return manifest, binding


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


class LabRemoteDispatchLane(CapBoundaryLane):
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
        #: the over-cap cycle's pre-planned run (index → run id).
        self._preplanned: dict[int, str] = {}
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

    async def _await_plan_note(self, issue_iid: int) -> str:
        """``/implement`` is already posted: await the plan note's run id
        (the app's plan is one cheap model call — bounded wait)."""

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
        return plan_run_id

    async def _drive_go(
        self, run_id: str, issue_iid: int, record: RemoteCycleRecord
    ) -> RemoteCycleRecord:
        """``/go`` posted: await the dispatch verdict (the REAL pipeline,
        or the typed blocked park) and measure queue wait / execution."""

        requested_at = datetime.now(UTC)
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
        return record

    async def dispatch(self, index: int) -> RemoteCycleRecord:
        record = RemoteCycleRecord(index=index)
        issue_iid = self.issues[index]
        # /implement → the app plans (one cheap model call per cycle).
        self._note(issue_iid, "@forge /implement")
        plan_run_id = await self._await_plan_note(issue_iid)
        if not plan_run_id:
            record.end_state = "no_plan"
            record.detail = "the plan note never arrived within the bounded wait"
            return record
        run_id = plan_run_id
        self.runs[index] = run_id
        record.run_id = run_id

        # /go — the dispatch entry: the app reserves the execution lease
        # (the observed limit) and dispatches the REAL pipeline.
        self._note(issue_iid, f"@forge /go {run_id}")
        record = await self._drive_go(run_id, issue_iid, record)
        if index == 0 and record.end_state == "dispatched":
            # The slow-control-ACK leg happens INSIDE the slot's window:
            # pause the run, let the command sit received (the slow lane),
            # then ack the ladder through the app's real endpoints.
            self.ack_measurement = await self._pause_and_slow_ack(run_id, issue_iid)
        return record

    async def plan_cycle(self, index: int) -> RemoteCycleRecord:
        """Phase A: ``/implement`` → the plan note. The plan holds NO
        lease and NO capacity (and its model call stays OUTSIDE the /go
        probe window — a deployment reality: plans are slow and
        fair-use-limited, dispatch verdicts at a full cap are seconds)."""

        record = RemoteCycleRecord(index=index)
        issue_iid = self.issues[index]
        self._note(issue_iid, "@forge /implement")
        plan_run_id = await self._await_plan_note(issue_iid)
        if not plan_run_id:
            record.end_state = "no_plan"
            record.detail = "the plan note never arrived within the bounded wait"
            return record
        self.runs[index] = plan_run_id
        self._preplanned[index] = plan_run_id
        record.run_id = plan_run_id
        record.end_state = "planned"
        record.detail = "planned, not dispatched — the /go is held for the probe phase"
        return record

    async def go_dropped(self, index: int) -> RemoteCycleRecord:
        """Phase B (the client seam): ``/go`` the PLANNED cycle — the app
        reserves the lease and dispatches its REAL pipeline — and this
        client then DELIBERATELY never processes the dispatch's answer
        (no pipeline lookup, no job identity). The cycle's occupancy is
        proven afterwards from the DURABLE lease row alone (read-only),
        never from the answer that was dropped. The teardown's branch
        cancel still runs afterwards (bounded spend — that is not the
        dispatch response, it is the cleanup)."""

        record = RemoteCycleRecord(index=index)
        issue_iid = self.issues[index]
        run_id = self._preplanned.get(index, "")
        if not run_id:
            record.end_state = "no_plan"
            record.detail = "the dropped-dispatch cycle was never planned"
            return record
        record.run_id = run_id
        requested_at = datetime.now(UTC)
        self._note(issue_iid, f"@forge /go {run_id}")
        # The response is DROPPED here: the ONLY thing this client reads
        # afterwards is the durable lease row (read-only) — the pipeline
        # and job the dispatch minted are never looked up by this leg.
        lease = None
        deadline = time.monotonic() + 240.0
        while time.monotonic() < deadline:
            lease = self._lease_row(run_id)
            if lease is not None and not lease["col3"]:
                break
            await asyncio.sleep(0.5)
        if lease is None or lease["col3"]:
            record.end_state = "no_dispatch_verdict"
            record.detail = "no durable lease row within the bounded wait (the dropped response resolved nothing)"
            return record
        acquired_at = datetime.fromisoformat(lease["col2"]) if lease["col2"] else None
        if acquired_at is not None and acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=UTC)
        record.lease_acquired = True
        record.queue_wait_s = (
            (acquired_at - requested_at).total_seconds() if acquired_at is not None else None
        )
        record.end_state = "dispatched_response_dropped"
        record.detail = (
            "the dispatch response was dropped at the client seam — occupancy proven from "
            "the durable lease row only (the pipeline/job identity was never read)"
        )
        self.notes.append(
            f"dropped-dispatch leg: run {run_id[:8]}… lease accounted "
            f"(queue wait {record.queue_wait_s and round(record.queue_wait_s, 1)}s)"
        )
        return record

    async def go_fill(self, index: int) -> RemoteCycleRecord:
        """Phase B (the fill): ``/go`` the PLANNED cycle and observe its
        dispatch — the pipeline and lane job are read, the lease row's
        acquired_at gives the queue wait."""

        record = RemoteCycleRecord(index=index)
        issue_iid = self.issues[index]
        run_id = self._preplanned.get(index, "")
        if not run_id:
            record.end_state = "no_plan"
            record.detail = "the fill cycle was never planned"
            return record
        record.run_id = run_id
        self._note(issue_iid, f"@forge /go {run_id}")
        return await self._drive_go(run_id, issue_iid, record)

    async def go_over_cap(self, index: int) -> RemoteCycleRecord:
        """Phase D: ``/go`` the ALREADY-PLANNED over-cap cycle the moment
        the cap is reached, and await its verdict — the typed park, or an
        honest dispatch if a slot freed in between."""

        record = RemoteCycleRecord(index=index)
        run_id = self._preplanned.get(index, "")
        if not run_id:
            record.end_state = "no_plan"
            record.detail = "the over-cap cycle was never planned"
            return record
        record.run_id = run_id
        issue_iid = self.issues[index]
        self._note(issue_iid, f"@forge /go {run_id}")
        record = await self._drive_go(run_id, issue_iid, record)
        self.notes.append(f"over-cap leg: run {run_id[:8]}… verdict {record.end_state}")
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


def _workflow_lane_ref(settings: Settings) -> str:
    """The workflow section's lane package: the LAB project's own
    ``FORGE_LANE_REF`` when it is at least the promoted release the
    redemption route was validated on, else the promoted tag itself.

    LIVE-FINDING (2026-09-26, the first workflow attempt): the lab's
    pinned ref (``2fbc321``, 2026-09-21) PREDATES the working-tree
    template's collector flags (``--collect-candidate`` /
    ``--expected-checkpoint-id``) — the lane job died
    ``collector_exit=2`` with the driver completed. The promoted v0.39.0
    package (``b521e1a``, the #343-validated redemption pairing with this
    exact template, sha256 ``3d74be37…``) carries both the collector
    flags and the redemption consumer, so the workflow section pins it
    when the lab's own ref is older, and NAMES the substitution."""

    response = gitlab_get(settings, f"/projects/{LAB_PROJECT_ID}/variables/FORGE_LANE_REF")
    pinned = ""
    if response.status_code == 200:
        pinned = str(response.json().get("value") or "").strip()
    if pinned == WORKFLOW_PROMOTED_LANE_REF or pinned == WORKFLOW_PROMOTED_LANE_SHA:
        return pinned
    log(
        f"workflow lane ref: the lab pins {pinned or 'nothing'} — substituting the promoted "
        f"{WORKFLOW_PROMOTED_LANE_REF} ({WORKFLOW_PROMOTED_LANE_SHA[:12]}…): the pinned ref "
        "predates this template's collector flags (live-found, recorded)"
    )
    return WORKFLOW_PROMOTED_LANE_REF


def setup_disposable_project(settings: Settings, name: str, *, workflow_shape: bool = False) -> int:
    """Create + seed the DISPOSABLE GitLab project (the R37-08 shape):
    pinned lane template, copied lane credentials, the forge webhook, the
    bot member, and a tiny smoke job whose completion feeds the
    failed-cancel leg. ``workflow_shape=True`` (the R40-15 envelope
    section) additionally seeds the project's ``.forge.yml`` work scope
    (so a reviewer's ``/fix`` can PROVE its claimed path inside the
    frozen spec's ``allowed_paths``) and copies the LAB project's own
    ``FORGE_LANE_REF`` verbatim — the lane package the lab owner
    validated the redemption route with, never a script-local guess."""

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
    actions: list[dict[str, str]] = [
        {"action": "create", "file_path": "README.md", "content": f"# {name}\n"},
        {"action": "create", "file_path": ".gitlab-ci.yml", "content": ci_yaml},
    ]
    if workflow_shape:
        # The SHIPPED lane template VERBATIM as the project's CI (the
        # #343-validated shape: its bootstrap carries the runner-redemption
        # consumer — the v0.36.0+patch template the other sections seed has
        # no redemption leg, and the R40-15 harness legs must match the
        # CURRENT lane mode). The seed receipt records the template's
        # sha256, byte-identical to the frozen profile's template.
        import hashlib as _hashlib

        template = (REPO_ROOT / "ci" / "templates" / "claude-sdk-lane.gitlab-ci.yml").read_text(
            encoding="utf-8"
        )
        workflow_ci_yaml = (
            template + "\n# A deliberately tiny always-green job: regular pipelines get a\n"
            "# fast success (the lane template's rules skip it for $FORGE_RUN_ID).\n"
            "smoke:\n"
            "  stage: test\n"
            "  image: python:3.13-slim\n"
            "  rules:\n"
            "    - if: '$FORGE_RUN_ID'\n"
            "      when: never\n"
            "    - when: on_success\n"
            "  script:\n"
            "    - python -c \"print('smoke ok')\"\n"
        )
        actions = [
            {"action": "create", "file_path": "README.md", "content": f"# {name}\n"},
            {"action": "create", "file_path": ".gitlab-ci.yml", "content": workflow_ci_yaml},
            {
                "action": "create",
                "file_path": ".forge.yml",
                # the approved work scope the /fix classification proves
                # its claimed path against (fnmatch globs — `**` spans `/`)
                "content": 'implement:\n  paths:\n    - "**"\n',
            },
        ]
        log(
            "workflow seed: the SHIPPED claude-sdk-lane template VERBATIM "
            f"(sha256 {_hashlib.sha256(template.encode()).hexdigest()[:16]}…) "
            "+ the smoke job + the broad .forge.yml work scope"
        )
    commit = gitlab_post(
        settings,
        f"/projects/{project_id}/repository/commits",
        json={
            "branch": "main",
            "commit_message": "seed: deployment-ops disposable project (R37-20)",
            "actions": actions,
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
        ("FORGE_LANE_REF", _workflow_lane_ref(settings) if workflow_shape else LANE_REF_SHA),
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
# Section 2b — the R38-18 lost-response-at-the-cap arm (same disposable
# project as the occupancy section)
# ---------------------------------------------------------------------------


async def section_cap_arm(
    settings: Settings,
    *,
    profile_binding: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """A dropped native dispatch response + the concurrency cap reached
    immediately, on the REAL control plane (the review's negative test
    1). Runs on its OWN disposable project: the app's fair-use gate
    counts a user's runs per project per hour (6), and the occupancy
    section already spent that budget on its own project — a fresh
    disposable project gives the arm its own window (and a clean
    teardown). Every cycle is PLANNED first (plans hold no lease), then
    one ``/go`` drops its dispatch response at the client seam while
    the fill ``/go`` cycles bring the project to its observed limit, the
    cycle after the cap parks typed, and the dropped run's occupancy
    keeps consuming capacity until the deployment's own reconciler
    resolves it by observation."""

    name = f"forge-ops-319-cap-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    project_id = setup_disposable_project(settings, name)
    lane = LabRemoteDispatchLane(settings, project_id, cycles=0)
    limit = lane.limit
    lane.cycles = limit + 1
    try:
        for index in range(limit + 1):
            created = gitlab_post(
                settings,
                f"/projects/{project_id}/issues",
                json={
                    "title": f"cap-arm task {index + 1}: dropped dispatch at the cap",
                    "description": TASK_BODY,
                },
            )
            if created.status_code not in (200, 201):
                raise Refused(f"issue creation failed: {created.text[:200]}")
            lane.issues.append(int(created.json()["iid"]))
        log(
            f"cap_arm: own disposable project {project_id}, {limit + 1} issues created — "
            f"every cycle planned first, then one dropped dispatch response, the cap "
            f"({limit}) reached immediately, one cycle over the cap"
        )
        outcome = await drill_lost_response_at_cap(
            lane, profile_binding=profile_binding, reconcile_timeout_s=300.0, sample_interval_s=1.0
        )
        document = outcome.as_document()
        document["scope"] = DEPLOYMENT_DRILL_SCOPE
        document["lane_notes"] = lane.notes
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
        return document, project_id
    except Exception:
        # The arm's own project never leaks past a failed arm.
        try:
            gitlab_delete(settings, f"/projects/{project_id}")
        except httpx.HTTPError:
            pass
        raise


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
# Section 3b — the R38-18 mismatched-restore preflight arm
# ---------------------------------------------------------------------------


async def section_mismatched_restore(
    work_dir: Path, *, profile_binding: dict[str, Any], expected_schema_head: str
) -> dict[str, Any]:
    """Mismatched metadata/blob snapshots restored into DISPOSABLE
    installations (the review's negative test 3): the preflight refuses
    typed BEFORE any new model turn — the halves consistency the backup
    contract enforces, plus the schema-head bind against the frozen
    profile's head (an installation declaring the profile's predecessor
    is refused, never silently upgraded)."""

    outcome = await drill_mismatched_restore_preflight(
        work_dir / "mismatched-restore",
        profile_binding=profile_binding,
        expected_schema_head=expected_schema_head,
    )
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    signal = document["signals"]["preflight.restore_gate"]
    log(
        f"mismatched_restore: halves refused {signal['refusals']['backup-halves']}, "
        f"schema {expected_schema_head}-bind refused {signal['refusals']['schema-head']}, "
        f"model turns before refusals {signal['model_turns_before_refusals']}"
    )
    return document


# ---------------------------------------------------------------------------
# Section 5b — the R38-18 volume-fill-during-pause arm
# ---------------------------------------------------------------------------


async def section_volume_fill(
    work_dir: Path, *, profile_binding: dict[str, Any], paused_run_id: str = ""
) -> dict[str, Any]:
    """The checkpoint volume filled to the configured safety threshold
    while a run is PAUSED with a pinned checkpoint (the review's negative
    test 2), on a quota-tmp-dir store shape — a real disk is NEVER
    filled; the typed-refusal path is what is measured. If the occupancy
    section paused a REAL run, that run's pins on the deployment's real
    store are observed READ-ONLY as context (never a pass/fail input —
    the pinned-survives proof runs on the disposable store)."""

    outcome = await drill_volume_fill_during_pause(
        work_dir / "volume-fill",
        profile_binding=profile_binding,
        safety_threshold_bytes=VOLUME_FILL_SAFETY_THRESHOLD_BYTES,
    )
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    deployment_pins: dict[str, Any] = {"observed": False, "run": paused_run_id or None}
    if paused_run_id and STORE_ROOT.is_dir():
        from forge.adaptive.checkpoint_repository import FilesystemCheckpointRepository

        try:
            real_store = FilesystemCheckpointRepository(STORE_ROOT)
            pins = await real_store.pins(paused_run_id)
            deployment_pins.update(
                {
                    "observed": True,
                    "pins": len(pins),
                    "note": "read-only observation of the real store's pin records",
                }
            )
        except Exception as exc:  # noqa: BLE001 — context only, recorded honestly
            deployment_pins["note"] = f"read-only pin observation failed: {type(exc).__name__}"
    document["deployment_paused_run_pins"] = deployment_pins
    signal = document["signals"]["storage.volume_fill"]
    log(
        f"volume_fill: {signal['typed_refusals']} typed refusals at "
        f"{signal['store_bytes_at_refusal']}B (threshold {signal['safety_threshold_bytes']}B), "
        f"pinned WIP survives {signal['pinned_wip_survives']}"
    )
    return document


# ---------------------------------------------------------------------------
# Section 5c — the R38-18 pause/cancel percentiles measurement
# ---------------------------------------------------------------------------


async def section_percentiles(
    work_dir: Path, *, profile_binding: dict[str, Any], control_objective_s: float
) -> dict[str, Any]:
    """Pause/cancel responsiveness with STATED percentiles and scope under
    upload + slow-provider load, through the REAL lane-control endpoints
    over a disposable database (never the lab's)."""

    outcome = await drill_pause_cancel_percentiles(
        work_dir / "percentiles",
        profile_binding=profile_binding,
        control_objective_s=control_objective_s,
    )
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    signal = document["signals"]["control.pause_cancel_percentiles_s"]
    log(
        f"percentiles: control p50 {signal['control']['p50_s']}s "
        f"p95 {signal['control']['p95_s']}s (n={signal['control']['n']}), "
        f"cancel-under-slow-provider p95 {signal['cancel_under_slow_provider']['p95_s']}s"
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
        # "Healthy" for THIS drill is the objective it asserts: the app keeps
        # SERVING with its core dependencies (database, redis) up and the
        # fences never disabled. The app's own litellm axis embeds a LIVE
        # model roundtrip behind a 5s probe timeout — under real gateway
        # latency the status flaps ok/degraded while the app itself is fine,
        # so that axis is external-dependency latency, not app health (it
        # stays visible in the drill's own dispatch signals). Anything else
        # (no answer, database/redis down) fails the predicate.
        try:
            document = app_health()
        except httpx.HTTPError:
            return False
        if document.get("database") != "ok" or document.get("redis") != "ok":
            return False
        return document.get("status") in {"ok", "degraded"}

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
# Section 7 (R40-15 / #351) — the operating envelope from the SELECTED
# workflow's own shape: a LIVE cycle with a review round on a disposable
# project, plus the drill-level arms (partition, degradation parking,
# redemption harness leg, workflow restore)
# ---------------------------------------------------------------------------


#: The deployment's persisted credential-binding registry (bind-mounted
#: at ``/app/data``; the app re-reads it on EVERY dispatch command and
#: redemption — the #343 rotation arm proved the live semantics).
CREDENTIAL_BINDINGS_PATH = REPO_ROOT / "data" / "credential-bindings.json"

#: The promoted lane package the redemption route was validated on
#: (#343): the workflow section pins it whenever the lab's own
#: ``FORGE_LANE_REF`` is older (the live-found collector-flag skew).
WORKFLOW_PROMOTED_LANE_REF = "v0.39.0"
WORKFLOW_PROMOTED_LANE_SHA = "b521e1a"


def _subject_of(project_id: int) -> str:
    return f"gitlab/-/{project_id}"


def bind_disposable_subject(project_id: int, *, bound_by: str) -> dict[str, Any]:
    """The OPERATOR action that binds the disposable project's canonical
    subject to the broker-held model credential (refs only, never a
    value) — the same registry decision the #343 qualification made for
    its own disposable subject. The file is rewritten atomically; the
    app re-reads it per dispatch/redemption."""

    document = json.loads(CREDENTIAL_BINDINGS_PATH.read_text(encoding="utf-8"))
    subject = _subject_of(project_id)
    bindings = [entry for entry in document.get("bindings", []) if entry.get("subject") != subject]
    binding = {
        "schema": "forge.project.credential-binding/2",
        "subject": subject,
        "provider": "anthropic-gateway",
        "credential_ref": "env:ANTHROPIC_AUTH_TOKEN",
        "env_var": "ANTHROPIC_AUTH_TOKEN",
        "project_id": project_id,
        "revision": 1,
        "bound_at": _now_iso(),
        "bound_by": bound_by,
        "revoked_at": None,
    }
    bindings.append(binding)
    document["bindings"] = bindings
    temporary = CREDENTIAL_BINDINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(CREDENTIAL_BINDINGS_PATH)
    return {"subject": subject, "credential_ref": binding["credential_ref"], "revision": 1}


def unbind_disposable_subject(project_id: int) -> None:
    """The teardown half of the operator action: the disposable subject's
    binding leaves the registry (the history block is untouched — every
    past decision stays visible)."""

    document = json.loads(CREDENTIAL_BINDINGS_PATH.read_text(encoding="utf-8"))
    subject = _subject_of(project_id)
    document["bindings"] = [
        entry for entry in document.get("bindings", []) if entry.get("subject") != subject
    ]
    temporary = CREDENTIAL_BINDINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(CREDENTIAL_BINDINGS_PATH)


class LabWorkflowEnvelopeLane(WorkflowShapeLane):
    """The REAL deployment's selected workflow, driven natively for the
    R40-15 envelope: issue → ``@forge /implement`` (the app plans — one
    cheap planner call) → ``@forge /go`` (the app reserves the execution
    lease, MINTS the operation grant and dispatches a REAL pipeline on
    runner 4; the lane bootstrap REDEEMS through the lane-control
    endpoint — the current ``runner-redemption`` lane mode) → the lane
    completes the trivial task → ``ready_for_human`` with its Draft MR →
    the reviewer's ``/fix`` note → the linked review ROUND admitted and
    its child dispatched (redemption-mode again) → the child's own
    readiness. Every stage moment is a DURABLE timestamp re-read from
    the control plane's rows, never a client clock."""

    def __init__(self, settings: Settings, project_id: int) -> None:
        self.settings = settings
        self.project_id = project_id
        app_env = inspect_container("forge-app")["env"]
        raw_limit = app_env.get("FORGE_ADMISSION_MAX_ACTIVE_PER_PROJECT", "").strip()
        raw_queued = app_env.get("FORGE_ADMISSION_MAX_QUEUED_RUNS", "").strip()
        raw_user = app_env.get("FORGE_ADMISSION_USER_RUNS_PER_HOUR", "").strip()
        self.limit = int(raw_limit) if raw_limit else 3
        self.queued_limit = int(raw_queued) if raw_queued else 10
        self.user_hour_limit = int(raw_user) if raw_user else 6
        self.notes: list[str] = []
        self.stopped_early = ""

    # -- durable reads through the read-only psql seam -------------------

    def _run_row(self, run_id: str) -> dict[str, str] | None:
        return (
            psql_select(
                "SELECT status, mr_iid, created_at, updated_at FROM flow_runs "
                f"WHERE id = '{run_id}' LIMIT 1"
            )
            or [None]
        )[0]

    async def _await_row(self, sql: str, *, timeout_s: float, interval_s: float = 5.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            rows = psql_select(sql)
            if rows:
                return rows[0]
            await asyncio.sleep(interval_s)
        rows = psql_select(sql)
        return rows[0] if rows else None

    async def _await_status(self, run_id: str, statuses: list[str], *, timeout_s: float):
        deadline = time.monotonic() + timeout_s
        row: dict[str, str] | None = None
        while time.monotonic() < deadline:
            row = self._run_row(run_id)
            if row and row["col0"] in statuses:
                return row
            await asyncio.sleep(10.0)
        return row

    async def _await_plan_note(self, issue_iid: int) -> str:
        plan_run_id = ""
        deadline = time.monotonic() + 420.0
        while time.monotonic() < deadline:
            response = gitlab_get(
                self.settings, f"/projects/{self.project_id}/issues/{issue_iid}/notes"
            )
            response.raise_for_status()
            for note in reversed(response.json()):
                body = str(note.get("body", ""))
                if note.get("author", {}).get("username") == "forge" and "/go " in body:
                    match = RUN_ID_RE.search(body)
                    if match is not None:
                        plan_run_id = match.group(1)
                        break
            if plan_run_id:
                break
            await asyncio.sleep(10.0)
        return plan_run_id

    def _issue_note(self, issue_iid: int, body: str) -> None:
        response = gitlab_post(
            self.settings,
            f"/projects/{self.project_id}/issues/{issue_iid}/notes",
            json={"body": body},
        )
        if response.status_code not in (200, 201):
            raise Refused(f"issue note failed: {response.text[:200]}")

    def _mr_note(self, mr_iid: int, body: str) -> dict[str, Any]:
        response = gitlab_post(
            self.settings,
            f"/projects/{self.project_id}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        )
        if response.status_code not in (200, 201):
            raise Refused(f"MR note failed: {response.text[:200]}")
        return response.json()

    def _forge_mr_reply(self, mr_iid: int, *, since_minutes: float = 30.0) -> list[str]:
        response = gitlab_get(
            self.settings, f"/projects/{self.project_id}/merge_requests/{mr_iid}/notes"
        )
        response.raise_for_status()
        return [
            str(note.get("body", ""))
            for note in response.json()
            if note.get("author", {}).get("username") == "forge"
        ]

    # -- the WorkflowShapeLane seam ---------------------------------------

    async def workflow_cycle(self, index: int) -> WorkflowCycleRecord:
        record = WorkflowCycleRecord(index=index)
        # 1. the intake: the issue (the drill IS the requester)
        created = gitlab_post(
            self.settings,
            f"/projects/{self.project_id}/issues",
            json={
                "title": f"envelope workflow {index + 1}: append one line, then a review round",
                "description": TASK_BODY,
            },
        )
        if created.status_code not in (200, 201):
            raise Refused(f"issue creation failed: {created.text[:200]}")
        issue = created.json()
        record.issue_iid = int(issue["iid"])
        record.stages["issue_created"] = str(issue.get("created_at") or "")
        # 2. /implement → the app plans (one cheap planner call)
        self._issue_note(record.issue_iid, "@forge /implement")
        run_id = await self._await_plan_note(record.issue_iid)
        if not run_id:
            record.end_state = "no_plan"
            record.detail = "the plan note never arrived within the bounded wait"
            self.stopped_early = record.end_state
            return record
        record.run_id = run_id
        plan_row = self._run_row(run_id)
        record.stages["plan_ready"] = str(plan_row["col3"]) if plan_row else ""
        # 3. /go — the redemption-mode dispatch: lease + grant minted
        # BEFORE the provider call + the lane's redemption
        go_at = _now_iso()
        self._issue_note(record.issue_iid, f"@forge /go {run_id}")
        grant_row = await self._await_row(
            "SELECT grant_id, created_at FROM operation_grants "
            f"WHERE work_id = '{run_id}' ORDER BY created_at DESC LIMIT 1",
            timeout_s=300.0,
        )
        record.stages["go_note"] = go_at
        if grant_row is not None:
            record.stages["grant_minted"] = grant_row["col1"]
        redemption_row = await self._await_row(
            "SELECT receipt_id, created_at FROM credential_redemptions "
            f"WHERE work_id = '{run_id}' ORDER BY created_at DESC LIMIT 1",
            timeout_s=600.0,
        )
        if redemption_row is not None:
            record.stages["redeemed"] = redemption_row["col1"]
        record.redemption = {
            "grant_minted": grant_row is not None,
            "redeemed": redemption_row is not None,
        }
        # 4. the lane completes the trivial task → ready_for_human
        ready_row = await self._await_status(
            run_id, ["ready_for_human", "failed", "blocked", "cancelled"], timeout_s=1500.0
        )
        if ready_row is None or ready_row["col0"] != "ready_for_human":
            record.end_state = f"lane_outcome_{(ready_row or {}).get('col0', 'unknown')}"
            record.detail = f"the parent run ended {(ready_row or {}).get('col0')}"
            self.stopped_early = record.end_state
            return record
        record.stages["ready_for_human"] = ready_row["col3"]
        record.mr_iid = int(ready_row["col1"]) if ready_row["col1"] else None
        if record.mr_iid is None:
            record.end_state = "no_mr"
            self.stopped_early = record.end_state
            return record
        # 5. the reviewer's /fix — the app's real MR-note ingress
        record.stages["fix_note"] = _now_iso()
        self._mr_note(record.mr_iid, "/fix also append the marker word `NOTES.md`")
        round_row = await self._await_row(
            "SELECT id, child_run_id, round_number, created_at FROM review_rounds "
            f"WHERE parent_run_id = '{run_id}' ORDER BY created_at DESC LIMIT 1",
            timeout_s=300.0,
        )
        if round_row is None:
            replies = self._forge_mr_reply(record.mr_iid)
            record.end_state = "no_round"
            record.detail = f"no review_rounds row; the forge replies said: {replies[-1][:160] if replies else 'nothing'}"
            self.stopped_early = record.end_state
            return record
        record.round_id = round_row["col0"]
        record.child_run_id = round_row["col1"]
        record.round_number = int(round_row["col2"] or 0)
        record.stages["round_admitted"] = round_row["col3"]
        # 6. the round child's own delivery (redemption-mode again)
        child_row = await self._await_status(
            record.child_run_id,
            ["ready_for_human", "failed", "blocked", "cancelled"],
            timeout_s=1500.0,
        )
        record.stages["child_ready_for_human"] = (
            child_row["col3"]
            if child_row is not None and child_row["col0"] == "ready_for_human"
            else ""
        )
        record.end_state = (
            "workflow_complete"
            if record.stages["child_ready_for_human"]
            else (f"child_outcome_{(child_row or {}).get('col0', 'unknown')}")
        )
        if record.end_state != "workflow_complete":
            self.stopped_early = record.end_state
        record.envelope = {"slots": sum((await self.occupancy_snapshot()).values())}
        self.notes.append(
            f"cycle {index}: run {run_id[:8]}… round {record.round_number} "
            f"(child {str(record.child_run_id)[:8]}…) — {record.end_state}"
        )
        return record

    async def amend_budget(
        self, record: WorkflowCycleRecord, *, axis: str, amount: float, command_id: str, reason: str
    ) -> dict[str, Any]:
        # The LIVE amendment rides the review-only continuation, which by
        # contract requires a run parked in reviewing with a REAL
        # exhausted review budget — manufacturing that state on the lab
        # would be exactly the silent re-plan the #340 guard refuses. The
        # applied+replay proof stands in the drill-level tests and the
        # workflow-restore drill; the live leg says so honestly.
        return {
            "exercised": False,
            "reason": (
                "the live amendment path (continue_review_only) requires a naturally "
                "exhausted review budget; the durable applied+replay proof is "
                "drill-level (deployment_workflow_restore + tests)"
            ),
        }

    async def over_intake_probe(self) -> dict[str, Any]:
        from forge.adaptive.admission import AdmissionPolicy, check_admission

        rows = psql_select(
            "SELECT count(*) FROM flow_runs WHERE provider = 'gitlab' "
            f"AND project_id = {self.project_id} "
            "AND (evidence->>'requested_by') IS NOT NULL "
            "AND created_at >= now() - interval '1 hour'"
        )
        observed = int(rows[0]["col0"]) if rows else 0
        # The deployment's own gate on the deployment's own counts, at the
        # limit-1 request shape: what the NEXT request would face once the
        # hourly per-user bound is reached.
        decision = check_admission(
            AdmissionPolicy(
                max_active_per_project=self.limit,
                max_queued_runs=self.queued_limit,
                max_user_runs_per_hour=self.user_hour_limit,
            ),
            active_count=0,
            queued_count=0,
            issue_run_count=0,
            user_recent_count=self.user_hour_limit,
        )
        return {
            "observed_user_runs_last_hour": observed,
            "user_hour_limit": self.user_hour_limit,
            "allowed": decision.allowed,
            "refusal": decision.refusal.value if decision.refusal else None,
            "basis": (
                "the app's own check_admission gate over the deployment's own hourly "
                "count at the limit shape (zero model spend on the probe)"
            ),
        }

    async def conflicting_fix_probe(self, record: WorkflowCycleRecord) -> dict[str, Any]:
        if record.mr_iid is None:
            return {"refusal": "not_exercised_no_mr", "second_round_admitted": False}
        self._mr_note(record.mr_iid, "/fix and also touch `README.md` again")
        deadline = time.monotonic() + 120.0
        refusal = ""
        while time.monotonic() < deadline:
            replies = self._forge_mr_reply(record.mr_iid)
            for body in reversed(replies):
                lowered = body.lower()
                if "conflicting" in lowered or "one outstanding" in lowered:
                    refusal = "conflicting_correction"
                    break
                if "window" in lowered and "closed" in lowered:
                    refusal = "correction_window_closed"
                    break
            if refusal:
                break
            await asyncio.sleep(5.0)
        rows = psql_select(
            "SELECT count(*) FROM review_rounds rr WHERE rr.root_run_id = "
            f"(SELECT root_run_id FROM review_rounds WHERE id = '{record.round_id}')"
        )
        total_rounds = int(rows[0]["col0"]) if rows else 0
        return {
            "refusal": refusal or "no_typed_reply_observed",
            "second_round_admitted": total_rounds > 1,
            "rounds_in_lineage": total_rounds,
        }

    async def occupancy_snapshot(self) -> dict[str, int]:
        rows = psql_select(
            "SELECT native_intent_at, native_handle, draining_at FROM execution_leases "
            f"WHERE project_id = {self.project_id} AND released_at IS NULL"
        )
        counts: dict[str, int] = {}
        for row in rows:
            if row["col2"]:
                word = "draining"
            elif not row["col0"]:
                word = "never_dispatched"
            elif row["col1"]:
                word = "native_running"
            else:
                word = "dispatched_unknown"
            counts[word] = counts.get(word, 0) + 1
        return counts

    async def queue_snapshot(self) -> dict[str, int]:
        rows = psql_select(
            "SELECT status, created_at FROM flow_runs WHERE provider = 'gitlab' "
            f"AND project_id = {self.project_id} AND status IN "
            "('accepted', 'preflight', 'planning', 'waiting_approval', 'proposing', "
            "'waiting_harness', 'waiting_ci')"
        )
        oldest = 0.0
        now = datetime.now(UTC)
        for row in rows:
            created = datetime.fromisoformat(row["col1"].replace(" ", "T")) if row["col1"] else None
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                oldest = max(oldest, (now - created).total_seconds())
        return {"queued": len(rows), "oldest_age_s": round(oldest, 1)}

    async def storage_bytes(self) -> int:
        if not STORE_ROOT.is_dir():
            return 0
        return sum(path.stat().st_size for path in STORE_ROOT.rglob("*") if path.is_file())

    async def redemption_ledger(self) -> dict[str, Any]:
        grants = psql_select(
            "SELECT count(*) FROM operation_grants og JOIN flow_runs fr ON fr.id = og.work_id "
            f"WHERE fr.project_id = {self.project_id}"
        )
        redemptions = psql_select(
            "SELECT count(*) FROM credential_redemptions cr JOIN flow_runs fr "
            f"ON fr.id = cr.work_id WHERE fr.project_id = {self.project_id}"
        )
        unjoined = psql_select(
            "SELECT count(*) FROM credential_redemptions cr JOIN flow_runs fr "
            f"ON fr.id = cr.work_id WHERE fr.project_id = {self.project_id} "
            "AND NOT EXISTS (SELECT 1 FROM operation_grants og WHERE og.grant_id = cr.grant_id)"
        )
        return {
            "grants": int(grants[0]["col0"]) if grants else 0,
            "redemptions": int(redemptions[0]["col0"]) if redemptions else 0,
            "unjoined": int(unjoined[0]["col0"]) if unjoined else 0,
        }


async def section_workflow_envelope(
    settings: Settings, *, profile_binding: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """The R40-15 operating envelope from the SELECTED workflow's own
    shape, LIVE on the lab: one full cycle (issue → plan → redemption-mode
    dispatch → ready_for_human → /fix review round → the round child's
    readiness) on a DISPOSABLE project whose canonical subject the
    section binds to the broker-held credential for exactly its lifetime
    (the same operator action the #343 qualification took). The lane
    package is the LAB project's own current ``FORGE_LANE_REF``."""

    name = f"forge-ops-351-envelope-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    project_id = setup_disposable_project(settings, name, workflow_shape=True)
    binding: dict[str, Any] = {}
    try:
        binding = bind_disposable_subject(
            project_id, bound_by="R40-15 #351 workflow envelope section (operator action)"
        )
        log(
            f"workflow_envelope: disposable project {project_id} bound "
            f"({binding['subject']} → {binding['credential_ref']} rev {binding['revision']})"
        )
        lane = LabWorkflowEnvelopeLane(settings, project_id)
        outcome = await drill_workflow_envelope(lane, cycles=1, sample_interval_s=5.0)
        document = outcome.as_document()
        document["scope"] = DEPLOYMENT_DRILL_SCOPE
        document["lane_notes"] = lane.notes
        document["credential_binding"] = {
            **binding,
            "note": (
                "bound for the section's lifetime only; the teardown removes the binding "
                "(the registry history block is untouched)"
            ),
        }
        return document, project_id
    finally:
        try:
            unbind_disposable_subject(project_id)
        except (OSError, ValueError):
            log("workflow_envelope: binding teardown failed — the subject stays bound (named)")


async def section_drill_level(
    kind: str,
    work_dir: Path,
    *,
    profile_binding: dict[str, Any],
    expected_schema_head: str,
    mismatched_schema_head: str,
) -> dict[str, Any]:
    """The R40-15 drill-level arms on DISPOSABLE fixtures (zero lab
    writes, zero spend): the partition arm, the degradation-parking arm,
    the redemption harness leg and the workflow restore drill."""

    if kind == "partition":
        fixture = await build_fixture(work_dir / "partition")
        try:
            outcome = await drill_partition_occupancy(
                fixture, limit=3, cycles=6, partition_window_s=1.2, sample_interval_s=0.05
            )
        finally:
            await fixture.dispose()
    elif kind == "degradation_parking":
        fixture = await build_fixture(work_dir / "parking")
        try:
            outcome = await drill_degradation_parking(fixture)
        finally:
            await fixture.dispose()
    elif kind == "redemption_lane":
        outcome = await drill_redemption_lane(work_dir, profile_binding=profile_binding)
    elif kind == "workflow_restore":
        outcome = await drill_workflow_restore(
            work_dir,
            profile_binding=profile_binding,
            expected_schema_head=expected_schema_head,
            mismatched_schema_head=mismatched_schema_head,
        )
    else:  # pragma: no cover — the caller names one of the four
        raise Refused(f"unknown drill-level section kind {kind!r}")
    document = outcome.as_document()
    document["scope"] = DEPLOYMENT_DRILL_SCOPE
    return document


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

SECTIONS = (
    "topology",
    "occupancy",
    "cap_arm",
    "backup_restore",
    "mismatched_restore",
    "credentials",
    "degraded",
    "volume_fill",
    "percentiles",
    "rotation",
    "partition",
    "degradation_parking",
    "redemption_lane",
    "workflow_restore",
    "workflow_envelope",
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_deployment_ops",
        description=(
            "R38-18: measure the deployment's operating limits BOUND to the frozen "
            "supported profile (read-only + disposable)."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the SANITIZED published report to this path (the raw diagnostics stay private)",
    )
    parser.add_argument(
        "--private-dir",
        type=Path,
        default=PRIVATE_REPORT_DIR,
        help=(
            "where the FULL diagnostics report lands (OUTSIDE the repository — the #304 "
            f"discipline; default {PRIVATE_REPORT_DIR})"
        ),
    )
    parser.add_argument(
        "--sections",
        default="all",
        help=f"comma-separated sections (or 'all'): {', '.join(SECTIONS)}",
    )
    parser.add_argument("--cycles", type=int, default=4, help="dispatch cycles (default 4)")
    parser.add_argument(
        "--reviewer-wip-bound",
        type=int,
        default=DEFAULT_REVIEWER_WIP_BOUND,
        help=(
            "the STATED reviewer-WIP policy bound (concurrent reviewable WIP; a policy "
            f"field, never a measurement — default {DEFAULT_REVIEWER_WIP_BOUND})"
        ),
    )
    parser.add_argument(
        "--keep-project", action="store_true", help="keep the disposable GitLab project"
    )
    parser.add_argument("--keep-work", action="store_true", help="keep the disposable work dir")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    work_dir = Path(tempfile.mkdtemp(prefix="forge-deployment-ops-"))
    report: dict[str, Any] = {
        "schema": REPORT_STAMP,
        "issue": (
            "R38-18 (#319) — operating limits and recovery measured on the actual "
            "supported deployment, BOUND to the frozen profile (previous: R37-20 #301)"
        ),
        "generated_at": _now_iso(),
        "scope": DEPLOYMENT_DRILL_SCOPE,
        "read_only": True,
        "sections_requested": [],
        "topology": None,
        "profile": None,
        "drills": [],
        "refusals": [],
        "policy_findings": [],
        "service_objectives": None,
        "measured_limits": None,
        "reviewer_wip": None,
        "private_diagnostics": None,
        "runbook": "docs/operations/deployment-boundaries.md",
    }
    selected = (
        list(SECTIONS)
        if args.sections.strip().lower() == "all"
        else [name.strip() for name in args.sections.split(",") if name.strip()]
    )
    report["sections_requested"] = selected

    # -- the profile bind FIRST: every measurement below is evidence only
    # for the deployment the frozen profile's executed-lab bind names.
    manifest: dict[str, Any] = {}
    profile_binding: dict[str, Any] = {}
    expected_schema_head = "027"
    try:
        manifest, profile_binding = load_profile_binding()
        report["profile"] = profile_binding
    except Exception as exc:  # noqa: BLE001 — an unbound run is recorded, never retried green
        record = {"section": "profile", "reason": str(exc)[:400]}
        report["refusals"].append(record)
        log(f"   [REFUSED] profile: {record['reason']}")
    if manifest:
        control_plane = manifest.get("control_plane") or {}
        expected_schema_head = str(
            (control_plane.get("schema_revision") or {}).get("head")
            or (control_plane.get("executed_lab") or {}).get("deployed_schema_head")
            or expected_schema_head
        )

    def record_drill(document: dict[str, Any]) -> None:
        if profile_binding:
            # R38-18: EVERY drill row names the supported-profile manifest
            # digest it ran against (the sanitized projection keeps the
            # digest + qualification; the private report keeps the row).
            document["profile"] = dict(profile_binding)
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
    cap_document: dict[str, Any] | None = None
    ack_measurement: dict[str, Any] = {}
    paused_run_id = ""
    project_id: int | None = None
    disposable_projects: list[int] = []
    try:
        if "topology" in selected:
            log("== section: topology (read-only)")
            try:
                report["topology"] = section_topology(settings)
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("topology", exc)

        needs_project = "occupancy" in selected
        if needs_project:
            log("== section: the disposable project (real native jobs, immediate cancels)")
            name = f"forge-ops-319-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            try:
                project_id = setup_disposable_project(settings, name)
                disposable_projects.append(project_id)
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("disposable_project", exc)

        if "occupancy" in selected and project_id is not None:
            log("== section: occupancy (disposable project, real native jobs, immediate cancels)")
            try:
                occupancy_document = await section_occupancy(
                    settings, project_id, cycles=args.cycles
                )
                record_drill(occupancy_document)
                ack_measurement = dict(occupancy_document.get("slow_control_ack") or {})
                paused_run_id = str(ack_measurement.get("work_id") or "")
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("occupancy", exc)

        if "cap_arm" in selected:
            log(
                "== section: cap_arm (own disposable project; dropped dispatch response at the cap)"
            )
            try:
                cap_document, cap_project_id = await section_cap_arm(
                    settings, profile_binding=profile_binding
                )
                disposable_projects.append(cap_project_id)
                record_drill(cap_document)
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("cap_arm", exc)

        if "backup_restore" in selected:
            log("== section: backup_restore (read-only snapshot + disposable targets)")
            try:
                record_drill(await section_backup_restore(work_dir))
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("backup_restore", exc)

        if "mismatched_restore" in selected:
            log("== section: mismatched_restore (preflight refuses before any new model turn)")
            try:
                record_drill(
                    await section_mismatched_restore(
                        work_dir,
                        profile_binding=profile_binding,
                        expected_schema_head=expected_schema_head,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("mismatched_restore", exc)

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

        if "volume_fill" in selected:
            log("== section: volume_fill (quota tmp dir during a PAUSED run, pinned WIP)")
            try:
                record_drill(
                    await section_volume_fill(
                        work_dir, profile_binding=profile_binding, paused_run_id=paused_run_id
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("volume_fill", exc)

        if "percentiles" in selected:
            log("== section: percentiles (pause/cancel under upload + slow-provider load)")
            try:
                record_drill(
                    await section_percentiles(
                        work_dir, profile_binding=profile_binding, control_objective_s=60.0
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("percentiles", exc)

        if "rotation" in selected:
            log("== section: rotation (disposable configuration, generation-scoped refusal)")
            try:
                record_drill(await section_rotation(settings, work_dir))
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("rotation", exc)

        # -- the R40-15 (#351) operating-envelope arms --------------------
        envelope_document: dict[str, Any] | None = None
        for kind in ("partition", "degradation_parking", "redemption_lane", "workflow_restore"):
            if kind not in selected:
                continue
            log(f"== section: {kind} (disposable fixture, profile-bound)")
            try:
                record_drill(
                    await section_drill_level(
                        kind,
                        work_dir,
                        profile_binding=profile_binding,
                        expected_schema_head=expected_schema_head,
                        mismatched_schema_head="030",
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal(kind, exc)

        if "workflow_envelope" in selected:
            log(
                "== section: workflow_envelope (LIVE: the selected workflow with a review "
                "round, in the current lane mode)"
            )
            try:
                envelope_document, envelope_project_id = await section_workflow_envelope(
                    settings, profile_binding=profile_binding
                )
                disposable_projects.append(envelope_project_id)
                record_drill(envelope_document)
            except Exception as exc:  # noqa: BLE001 — a section failure is recorded, never fatal to the report
                record_refusal("workflow_envelope", exc)

        # -- the human-capacity discipline (a STATED POLICY FIELD) -------
        admission_limit = 3
        for document in report["drills"]:
            signal = (document.get("signals") or {}).get("execution.occupied_vs_limit") or {}
            if signal.get("limit"):
                admission_limit = int(signal["limit"])
                break
        if report["topology"]:
            observed_limit = report["topology"]["observed"]["admission"]["max_active_per_project"]
            admission_limit = int(observed_limit)
        reviewer_wip = reviewer_wip_bound_row(
            admission_limit=admission_limit, reviewer_wip_bound=args.reviewer_wip_bound
        )
        report["reviewer_wip"] = reviewer_wip
        if not reviewer_wip["coherent"]:
            report["policy_findings"].append(
                {
                    "policy": "reviewer_wip",
                    "finding": reviewer_wip["statement"],
                    "action": "lower the admission bound or grow review capacity before increasing load",
                }
            )
            log(f"   [POLICY] reviewer-WIP: {reviewer_wip['statement']}")

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

        # -- the measured-limits table (R38-18: the runbook's numbers,
        # bound to the frozen profile) -------------------------------
        cap_signal = ((cap_document or {}).get("signals") or {}).get(
            "execution.occupied_vs_limit"
        ) or {}
        percentile_signal = {}
        for document in report["drills"]:
            if document.get("drill") == "deployment_pause_cancel_percentiles":
                percentile_signal = (document.get("signals") or {}).get(
                    "control.pause_cancel_percentiles_s"
                ) or {}
        report["measured_limits"] = {
            "profile": {
                "manifest_digest": profile_binding.get("manifest_digest"),
                "qualification": profile_binding.get("qualification"),
            },
            "concurrency": {
                "limit": cap_signal.get("limit") or admission_limit,
                "peak_occupied": cap_signal.get("peak_occupied"),
                "occupied_at_cap": cap_signal.get("occupied_at_cap"),
                "occupancy_mix_at_cap": cap_signal.get("occupancy_mix_at_cap"),
                "over_cap_verdict": (
                    ((cap_document or {}).get("signals") or {})
                    .get("native.occupancy_unknown", {})
                    .get("over_cap_verdict")
                ),
                "scope": (
                    "one dropped native dispatch response + the cap reached immediately; "
                    "running/unknown/draining observed under the controlled failure "
                    "(per-drill tested_limits carry the exact N and waits)"
                ),
            },
            "pause_cancel_responsiveness": {
                "control_percentiles_s": percentile_signal.get("control"),
                "cancel_under_slow_provider_percentiles_s": percentile_signal.get(
                    "cancel_under_slow_provider"
                ),
                "scope": percentile_signal.get("scope"),
            },
            "command_latency_under_contention": {
                "control.received_to_applied_s": received_to_applied,
                "scope": (
                    "the slow-control-ACK window measured through the app's real "
                    "lane-control endpoints during the occupancy load; the machinery "
                    "percentiles are in pause_cancel_responsiveness"
                ),
            },
            "restore_time_s": {
                "measured": restore_seconds,
                "scope": "the deployment's CURRENT store size — remeasure as it grows; the number does not extrapolate",
            },
        }

        # -- the R40-15 operating envelope (the selected workflow's own
        # shape — the support agreement's §envelope numbers) ----------
        envelope_signals = (envelope_document or {}).get("signals") or {}
        partition_signal = {}
        parking_signal = {}
        redemption_lane_signal = {}
        restore_gate_signal = {}
        for document in report["drills"]:
            drill_name = document.get("drill")
            if drill_name == "deployment_partition_occupancy":
                partition_signal = (document.get("signals") or {}).get(
                    "execution.occupied_vs_limit"
                ) or {}
            if drill_name == "deployment_degradation_parking":
                parking_signal = (document.get("signals") or {}).get(
                    "provider_degradation.parking"
                ) or {}
            if drill_name == "deployment_redemption_lane":
                redemption_lane_signal = dict(document.get("signals") or {})
            if drill_name == "deployment_workflow_restore":
                restore_gate_signal = (document.get("signals") or {}).get(
                    "recovery.rto_observed"
                ) or {}
        report["measured_limits"]["operating_envelope"] = {
            "workflow_shape": envelope_signals.get("envelope.slots"),
            "queued_bound": envelope_signals.get("envelope.queued"),
            "per_user_intake": envelope_signals.get("envelope.intake"),
            "storage_growth": envelope_signals.get("envelope.storage"),
            "redemption_ledger": envelope_signals.get("envelope.redemption_ledger"),
            "separate_measures": {
                name: envelope_signals.get(name)
                for name in (
                    "measures.issue_to_reviewed_ready_s",
                    "measures.reviewer_wait_s",
                    "measures.command_to_applied_fix_s",
                    "measures.command_to_applied_amendment_s",
                    "measures.checkpoint_to_restored_s",
                )
                if envelope_signals.get(name) is not None
            },
            "partition_arm": partition_signal,
            "degradation_parking_arm": parking_signal,
            "redemption_harness_leg": redemption_lane_signal,
            "workflow_restore_gate": restore_gate_signal,
            "scope": (
                "the selected workflow's own shape (a review round, a guarded amendment, "
                "a redemption-mode dispatch) measured on the actual lab deployment — "
                "n=1 live cycle per run plus the drill-level arms; every measure carries "
                "its own n and percentiles are claimed only where the sample supports them"
            ),
        }
        report["summary"] = {
            "drills_run": len(report["drills"]),
            "passed": sum(1 for d in report["drills"] if d.get("outcome") == "pass"),
            "failed": sum(1 for d in report["drills"] if d.get("outcome") == "fail"),
            "refused_sections": len(report["refusals"]),
            "profile_qualification": (profile_binding or {}).get("qualification"),
            "policy_findings": len(report["policy_findings"]),
        }
    finally:
        if not args.keep_project:
            for disposable_id in disposable_projects:
                try:
                    response = gitlab_delete(settings, f"/projects/{disposable_id}")
                    if response.status_code in (200, 202, 204):
                        log(f"teardown: disposable project {disposable_id} deleted")
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
        args.out = REPO_ROOT / "qualification" / "deployment-ops-2026-09-25.json"
    settings = Settings()
    report = asyncio.run(_run(args, settings))

    # The FULL diagnostics report goes PRIVATE (outside the repository —
    # the #304 discipline); the PUBLISHED document is the sanitized
    # summary. Only the retention receipt travels with the public tree.
    private_path: Path | None = None
    try:
        args.private_dir.mkdir(parents=True, exist_ok=True)
        private_path = (
            args.private_dir
            / f"deployment-ops-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-full.json"
        )
        private_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        log(f"private report write failed ({exc}) — the sanitized summary still publishes")
    report["private_diagnostics"] = {
        "retained": private_path is not None,
        "location_class": (
            "outside the repository, access-controlled (never a public artifact)"
            if private_path is not None
            else "not retained (write failed)"
        ),
        "schema": REPORT_STAMP,
        "note": (
            "raw diagnostics (run/job/pipeline identifiers, per-cycle details, host "
            "paths, refusal stderr) live ONLY there — the public tree carries the "
            "sanitized summary (#304 discipline)"
        ),
    }

    published = summarize_for_publication(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(published, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(f"published report (sanitized): {args.out}")
    if private_path is not None:
        log(f"private diagnostics (full):  {private_path}")
    summary = report.get("summary", {})
    log(
        f"summary: {summary.get('passed', 0)}/{summary.get('drills_run', 0)} drills passed "
        f"({summary.get('failed', 0)} failed, {summary.get('refused_sections', 0)} section(s) "
        f"refused), profile {summary.get('profile_qualification')}, "
        f"{summary.get('policy_findings', 0)} policy finding(s)"
    )
    log(f"scope: {report['scope']}")
    failed = (
        summary.get("failed", 0) > 0
        or summary.get("refused_sections", 0) > 0
        or summary.get("profile_qualification") != PROFILE_QUALIFIED
        or summary.get("policy_findings", 0) > 0
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
