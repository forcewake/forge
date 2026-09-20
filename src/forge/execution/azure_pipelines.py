"""Azure Pipelines execution adapter (ADR-0024, milestone AZ-3).

The third execution adapter (ADR-0019/0024): the coding harness runs in the
TARGET project's ephemeral Azure Pipelines agent — no write credential, no
forge secret — and the candidate comes back as a pipeline artifact the
trusted publisher validates and publishes (ADR-0016). Contract per
ADR-0024 §6: ``launch`` (Runs API dispatch) → ``poll`` (run state/result +
artifact collection, mapped onto :class:`~forge.runs.backends.HarnessOutcome`)
→ ``cancel`` → ``reconcile_launch``, over a typed, JSON-serializable
:class:`AzurePipelinesHandle` journaled in the run's evidence.

Correlation (research §6.2): the Runs API dispatch response CARRIES the run
id — used directly; no discovery in the happy path. Azure DevOps Server
fallback: the run is discovered via the builds API
(:meth:`~forge.integrations.azure.AzureDevOpsClient.list_builds_by_repository`)
with a client-side match on the journaled run's branch + template
parameters within the dispatch-created window. Ground truth honored:
**builds has NO ``sourceVersion`` query parameter** — it exists only as a
response field, so it is never sent and never trusted for correlation
(research §6.6 correction #1); the lane's meta attempt-base check in
:meth:`AzurePipelinesExecutor.poll` is the real candidate-side pin.

Failure classification mirrors the Actions adapter
(:mod:`forge.execution.github_actions`): auth/quota/connectivity patterns
in the failed task's log mean infrastructure (research §6.4 timeline
recipe); the run ``result`` classifies the rest; deadline breaches are
``harness_timeout`` — never the code's fault. ``runId == buildId`` for
YAML pipeline runs (research §6.3), so the timeline/log/cancel calls key
off the journaled run id directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from forge.execution.github_actions import CandidateArchiveError, _extract_candidate
from forge.integrations.azure import AzureDevOpsClient, AzureDevOpsError
from forge.runs.backends import (
    _HARNESS_INFRASTRUCTURE_PATTERNS,
    HarnessOutcome,
)
from forge.runs.candidate import CandidateError, HarnessUsage, parse_unified_diff
from forge.runs.execution_profile import bootstrap_failed

logger = logging.getLogger(__name__)

#: Artifact name the lane template publishes
#: (``ci/templates/forge-lane.azure-pipelines.yml``, ``publish:`` step).
ARTIFACT_NAME_PREFIX = "forge-candidate-"

#: Log-tail bound when classifying a failed harness task (Actions parity).
LOG_TAIL_CHARS = 8000

#: Run.state values that mean "keep waiting" (documented RunState members,
#: research §6.2: inProgress | canceling | canceled | completed | notStarted
#: | postponed). Compared lower-cased; an empty state waits too.
_ACTIVE_RUN_STATES = frozenset({"", "inprogress", "notstarted", "postponed", "canceling"})

#: Run.result values meaning forge (or a human) cancelled the run —
#: infrastructure, never the code's fault (research §6.2/§6.5).
_CANCELED_RESULTS = frozenset({"canceled", "canceledbyuser"})


def artifact_name_for(run_id: str) -> str:
    """The candidate artifact name for *run_id* (template contract)."""
    return f"{ARTIFACT_NAME_PREFIX}{run_id}"


@dataclass(frozen=True)
class AzurePipelinesHandle:
    """The journaled execution handle (ADR-0024 §6: the opaque JSON string
    the caller stores in ``flow_runs.evidence`` so a run survives restarts).

    ``run_id`` is the PIPELINE run id — 0 until correlation succeeds
    (directly from the dispatch response, or via
    :meth:`AzurePipelinesExecutor.reconcile_launch` for the Server
    fallback; runId == buildId, research §6.3). ``branch`` is the FULL ref
    (``refs/heads/…``) dispatched as ``resources.repositories.self.refName``;
    ``forge_run_id`` is forge's own run id — the value dispatched as the
    ``run_id`` template parameter and embedded in the artifact name;
    ``repo_id`` is the repository id (GUID or name) the builds-API
    discovery filters on.
    """

    provider: str
    project: str
    repo_id: str
    pipeline_id: int
    run_id: int
    branch: str
    attempt_base_sha: str
    run_spec_digest: str
    driver: str
    model: str
    work_item_id: str
    forge_run_id: str
    started_at: str
    # B04: the envelope binding inputs — carried on the handle so a crash
    # recovery's re-dispatch renders the lane ENFORCED exactly like the
    # original dispatch (0/"" = legacy, renders unenforced).
    plan_note_id: int = 0
    envelope_digest: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> AzurePipelinesHandle:
        data = json.loads(raw)
        return cls(
            provider=str(data["provider"]),
            project=str(data["project"]),
            repo_id=str(data.get("repo_id") or ""),
            pipeline_id=int(data.get("pipeline_id") or 0),
            run_id=int(data.get("run_id") or 0),
            branch=str(data["branch"]),
            attempt_base_sha=str(data["attempt_base_sha"]),
            run_spec_digest=str(data.get("run_spec_digest") or ""),
            driver=str(data.get("driver") or ""),
            model=str(data.get("model") or ""),
            work_item_id=str(data.get("work_item_id") or ""),
            forge_run_id=str(data.get("forge_run_id") or ""),
            started_at=str(data["started_at"]),
            plan_note_id=int(data.get("plan_note_id") or 0),
            envelope_digest=str(data.get("envelope_digest") or ""),
        )

    def with_run_id(self, run_id: int) -> AzurePipelinesHandle:
        return replace(self, run_id=run_id)


class AzurePipelinesExecutor:
    """launch / poll / cancel / reconcile_launch over the Pipelines REST API.

    Duck-types the ADR-0015 backend seam's ``poll`` result type
    (:class:`HarnessOutcome`) so the reconciler treats all three CI lanes
    alike; the handle it consumes is the Azure Pipelines-shaped one above.
    Constructed with an injected :class:`AzureDevOpsClient` + settings —
    exactly how :class:`~forge.execution.github_actions.GitHubActionsExecutor`
    receives its client.
    """

    def __init__(self, client: AzureDevOpsClient, settings: Any) -> None:
        self._client = client
        self._settings = settings

    # -- launch ---------------------------------------------------------------

    async def launch(
        self,
        handle: AzurePipelinesHandle,
        *,
        parameters: dict[str, str] | None = None,
    ) -> AzurePipelinesHandle:
        """Dispatch the harness pipeline on the factory branch; correlate.

        The dispatch fires exactly once (non-idempotent upstream). The
        template parameters cross the REST boundary as strings (research
        §6.2) and match the lane template's queue-time ``parameters:``
        contract exactly. When the response carries no run id (Azure
        DevOps Server fallback), the run is discovered by branch +
        template parameters within the dispatch-created window.
        """
        template_parameters = {
            "run_id": handle.forge_run_id,
            "attempt_base": handle.attempt_base_sha,
            "driver": handle.driver,
            "model": handle.model,
            "work_item_id": handle.work_item_id,
            # B04: re-dispatches carry the envelope binding exactly like
            # the original dispatch (empty = legacy, renders unenforced).
            **(
                {
                    "plan_note_id": str(handle.plan_note_id),
                    "envelope_digest": handle.envelope_digest,
                    "spec_digest": handle.run_spec_digest,
                }
                if handle.plan_note_id and handle.envelope_digest
                else {}
            ),
            **(parameters or {}),
        }
        run = await self._client.run_pipeline(
            handle.project,
            handle.pipeline_id,
            ref_name=handle.branch,
            template_parameters=template_parameters,
        )
        if run.run_id:
            return handle.with_run_id(run.run_id)
        return await self._discover(handle, since=datetime.now(timezone.utc))

    async def reconcile_launch(self, handle: AzurePipelinesHandle) -> AzurePipelinesHandle:
        """Recover correlation after a lost/ambiguous launch (ADR-0005).

        Idempotent re-entry: a handle that already carries a run id is
        verified to still exist (404 → stays, the reconciler's deadline
        handles it); an uncorrelated handle re-runs the builds-API
        discovery against a generous window. Never dispatches again — one
        launch per intent.
        """
        if handle.run_id:
            try:
                await self._client.get_run(handle.project, handle.pipeline_id, handle.run_id)
            except AzureDevOpsError as exc:
                logger.warning(
                    "Pipeline run %d for %s no longer readable (%s) — keeping the handle",
                    handle.run_id,
                    handle.branch,
                    exc,
                )
            return handle
        started = _parse_started_at(handle.started_at)
        return await self._discover(handle, since=started)

    async def _discover(
        self, handle: AzurePipelinesHandle, *, since: datetime | None
    ) -> AzurePipelinesHandle:
        """Server fallback: find the dispatch run via the builds API.

        ``list_builds_by_repository`` sends ONLY documented query params —
        there is no ``sourceVersion`` filter (research §6.6 correction #1),
        so the correlation is a client-side match on the queue window + the
        dispatched branch + the template parameters carrying forge's run
        id. Ordering alone is never trusted.
        """
        try:
            builds = await self._client.list_builds_by_repository(
                handle.project,
                handle.repo_id,
                definitions=[handle.pipeline_id],
                min_time=(since - timedelta(minutes=5)) if since else None,
            )
        except AzureDevOpsError:
            logger.warning(
                "Build discovery failed for pipeline %d on %s (%s) — returning the handle uncorrelated",
                handle.pipeline_id,
                handle.branch,
                exc_info=True,
            )
            return handle
        for build in builds:  # newest queue time first
            if str(build.get("sourceBranch") or "") != handle.branch:
                continue
            if not _build_carries_run(build, handle.forge_run_id):
                continue
            run_id = int(build.get("id") or 0)
            if not run_id:
                continue
            logger.info("Pipeline harness run %d discovered for branch %s", run_id, handle.branch)
            return handle.with_run_id(run_id)
        logger.info(
            "No dispatch build found for pipeline %d on %s yet — discovery retries later",
            handle.pipeline_id,
            handle.branch,
        )
        return handle

    # -- poll -----------------------------------------------------------------

    async def poll(
        self, handle: AzurePipelinesHandle, *, now: datetime | None = None
    ) -> HarnessOutcome:
        """One poll pass: run state/result → running / candidate / failure."""
        if not handle.run_id:
            return HarnessOutcome.running()  # not yet correlated — keep waiting
        now = now or datetime.now(timezone.utc)
        try:
            run = await self._client.get_run(handle.project, handle.pipeline_id, handle.run_id)
        except AzureDevOpsError:
            logger.warning(
                "Pipeline run %d read failed — keeping the run waiting",
                handle.run_id,
                exc_info=True,
            )
            return HarnessOutcome.running()

        state = run.state.strip().lower()
        if state in _ACTIVE_RUN_STATES:
            return self._deadline_outcome(handle, now)
        result = (run.result or "").strip().lower()
        if not result:
            # A terminal-state run whose result has not landed yet (the
            # §6.5 caveat: state and result can lag independently) — keep
            # waiting rather than mis-classify.
            return self._deadline_outcome(handle, now)
        if result == "succeeded":
            return await self._collect_candidate(handle)
        if result == "succeededwithissues":
            # Success with warnings: the candidate is collected, the run
            # note travels in the summary (success-with-issues provenance).
            return await self._collect_candidate(handle, run_note="succeededWithIssues")
        if result in _CANCELED_RESULTS:
            # External cancellation is not the code's fault (ADR-0015).
            return HarnessOutcome.failed("infrastructure", "harness run canceled")
        if result == "failed":
            return await self._classify_failure(handle)
        return HarnessOutcome.failed(
            "infrastructure", f"harness run {state or 'finished'} with result {result or 'unknown'}"
        )

    def _deadline_outcome(self, handle: AzurePipelinesHandle, now: datetime) -> HarnessOutcome:
        """running before the durable deadline; harness_timeout past it.

        Deadline = journaled ``started_at`` + ``FORGE_HARNESS_TIMEOUT_SECONDS``
        (ADR-0013: budgets are enforced by forge, not by the agent pool —
        the job's own ``timeoutInMinutes`` is only the first line).
        """
        timeout = int(getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800)
        started = _parse_started_at(handle.started_at)
        if started is not None and _aware(now) > _aware(started) + timedelta(seconds=timeout):
            return HarnessOutcome.failed("infrastructure", "harness_timeout")
        return HarnessOutcome.running()

    async def _collect_candidate(
        self, handle: AzurePipelinesHandle, *, run_note: str = ""
    ) -> HarnessOutcome:
        """Artifact zip → CandidateBundle (ADR-0016 parse path).

        The attempt base compared against the artifact's meta comes from
        the TRUSTED handle (what forge pinned), never from the artifact
        alone; the publisher re-checks it at the write boundary.
        """
        wanted = artifact_name_for(handle.forge_run_id or str(handle.run_id))
        try:
            payload = await self._client.download_run_artifact(
                handle.project, handle.pipeline_id, handle.run_id, wanted
            )
        except AzureDevOpsError:
            # Retention expired before collection, or the publish never ran
            # — both mean the candidate is gone (research §6.3).
            return HarnessOutcome.failed("code", "harness_artifact_missing")

        try:
            diff_text, meta = _extract_candidate(payload)
        except CandidateArchiveError as exc:
            return HarnessOutcome.failed("code", f"harness_artifact_invalid: {exc}")

        # The lane writes attempt_base (the dispatch parameter name); accept
        # the Actions lane's key too so both archives extract alike.
        reported_base = str(meta.get("attempt_base") or meta.get("attempt_base_oid") or "")
        if reported_base != handle.attempt_base_sha:
            return HarnessOutcome.failed(
                "code",
                "harness_attempt_base_mismatch: artifact base "
                f"{reported_base[:8] or '<none>'}, expected {handle.attempt_base_sha[:8] or '<none>'}",
            )

        driver_exit = str(meta.get("exit") or "completed").strip() or "completed"
        usage = HarnessUsage.from_meta(
            meta.get("usage"),
            driver=str(meta.get("driver") or handle.driver),
            model=str(meta.get("model") or ""),
        )
        try:
            bundle = parse_unified_diff(
                diff_text,
                attempt_base_oid=handle.attempt_base_sha,
                driver_exit=driver_exit,
                usage=usage,
            )
        except CandidateError as exc:
            return HarnessOutcome.failed("code", f"harness_candidate_invalid: {exc.reason}: {exc}")

        if driver_exit != "completed":
            # A18: a FAILED environment bootstrap is lane infrastructure/
            # config — the environment never matched the approved execution
            # profile, which is never the code's fault and never a repair
            # candidate. The meta's additive ``bootstrap`` field carries the
            # lane's own classification; it overrides the code verdict.
            if bootstrap_failed(meta):
                detail = " with no changes" if bundle.is_empty else ""
                return HarnessOutcome.failed(
                    "infrastructure",
                    f"harness_bootstrap_failed (driver exit={driver_exit}{detail})",
                )
            # The driver itself reported failure: never adopt a possibly
            # partial working tree, whatever it managed to change.
            detail = " with no changes" if bundle.is_empty else ""
            return HarnessOutcome.failed(
                "code", f"harness_driver_failed (exit={driver_exit}{detail})"
            )
        if bundle.is_empty:
            return HarnessOutcome.failed("code", "harness_no_changes")
        summary = str(meta.get("summary") or "")
        if run_note:
            summary = f"{summary} [harness run {run_note}]".strip()
        return HarnessOutcome.change_candidate(bundle, summary=summary)

    async def _classify_failure(self, handle: AzurePipelinesHandle) -> HarnessOutcome:
        """Failed run → code vs infrastructure, via the build timeline.

        Timeline records with ``type == "Task"`` and ``result == "failed"``
        name the failed tasks (research §6.4 recipe); their logs are
        tail-bounded and scanned for auth/quota/connectivity patterns —
        those mean the *environment* failed. No failed task readable, or a
        log with no blame at all, is treated as infrastructure too — unknown
        means the evidence does not blame the code.
        """
        detail = "harness run failed"
        try:
            timeline = await self._client.get_timeline(handle.project, handle.run_id)
        except AzureDevOpsError:
            timeline = {}
        failed_tasks = [
            record
            for record in _timeline_records(timeline)
            if str(record.get("type") or "").lower() == "task"
            and str(record.get("result") or "").lower() == "failed"
        ]
        if not failed_tasks:
            return HarnessOutcome.failed("infrastructure", detail)
        record = failed_tasks[0]
        detail = f"harness task {record.get('name') or record.get('identifier') or record.get('id')} failed"
        log = ""
        log_id = _record_log_id(record)
        if log_id is not None:
            try:
                log = await self._client.get_task_log(handle.project, handle.run_id, log_id)
            except AzureDevOpsError:
                log = ""
        lowered = log[-LOG_TAIL_CHARS:].lower()
        if any(pattern in lowered for pattern in _HARNESS_INFRASTRUCTURE_PATTERNS):
            return HarnessOutcome.failed("infrastructure", detail)
        return HarnessOutcome.failed("code", detail)

    # -- cancel ---------------------------------------------------------------

    async def cancel(self, handle: AzurePipelinesHandle) -> None:
        """Best-effort cancel of the pipeline run (research §6.5).

        The Runs area has NO cancel — cancellation goes through the Builds
        area (``PATCH {"status": "cancelling"}``; runId == buildId). A
        ``cancelling`` build may leave queued jobs behind: the reconciler's
        poll loop runs until ``result == canceled`` rather than trusting
        the PATCH. Tolerates 404/409 (already finished / never started).
        """
        if not handle.run_id:
            return
        try:
            await self._client.cancel_build(handle.project, handle.run_id)
        except AzureDevOpsError as exc:
            if exc.status_code in (404, 409):
                return  # nothing left to cancel
            raise


def _build_carries_run(build: dict[str, Any], forge_run_id: str) -> bool:
    """Whether *build* was queued with *forge_run_id* in its parameters.

    The Build object carries the queued template parameters either as the
    ``templateParameters`` object or as the serialized ``parameters``
    string (research §6.2: ``Build.parameters`` is documented as string).
    Matched defensively: object → exact ``run_id`` key; string → substring
    (the serialized form's exact schema is Server-version dependent).
    """
    if not forge_run_id:
        return False
    raw = build.get("templateParameters")
    if isinstance(raw, dict):
        return str(raw.get("run_id") or "") == forge_run_id
    if not isinstance(raw, str):
        raw = build.get("parameters")
    return isinstance(raw, str) and forge_run_id in raw


def _timeline_records(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """The timeline's records, defensively (research §6.4/§10.12)."""
    records = timeline.get("records") if isinstance(timeline, dict) else None
    return [record for record in records or [] if isinstance(record, dict)]


def _record_log_id(record: dict[str, Any]) -> int | None:
    """The ``log.id`` of a timeline record, or None when it has no log."""
    log = record.get("log")
    if not isinstance(log, dict):
        return None
    raw = log.get("id")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _parse_started_at(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
