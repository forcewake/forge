"""Pluggable implementer backends (ADR-0015/0016).

The implementer step of a run is pluggable; the controller owns the lifecycle
unchanged (ADR-0004). Two backends:

- ``BuiltinBackend`` — today's LLM → ChangeSet → Commits API path (ADR-0001).
  The produced ChangeSet is wrapped as a :class:`CandidateBundle` and routed
  through the trusted publisher (ADR-0016 §2) so every backend crosses the
  same validation → publication boundary.
- ``CITharnessBackend`` — delegates implementation to a coding harness
  executing as a job in the *target project's* CI (never on forge
  infrastructure, ADR-0002/0015). The job runs proposal-only (ADR-0016): it
  checks out the frozen attempt base (``FORGE_ATTEMPT_BASE``), receives NO
  write credential, and uploads ``.forge/candidate.diff`` +
  ``.forge/candidate.meta.json`` as CI artifacts. Forge downloads the
  artifacts and turns the diff into a :class:`CandidateBundle` — the
  harness's own success claim is never trusted.

Trust boundary (ADR-0016): the only things that may become a commit are the
bytes of the downloaded candidate diff, validated and written by the trusted
publisher. Harness-level failures (auth, quota, runner, timeout) classify as
``infrastructure``, never ``code``.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable import FlowRun, as_aware_utc
from forge.durable.identity import factory_branch
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository.writer import ChangesetWriter
from forge.runs.candidate import (
    CandidateBundle,
    CandidateError,
    HarnessUsage,
    attempt_base_for,
    bundle_from_changeset,
    parse_unified_diff,
)
from forge.runs.ci_contract import classify_failure
from forge.runs.publisher import publish_candidate

logger = logging.getLogger(__name__)

#: Harness failure kinds (ADR-0015): infrastructure/config failures never
#: trigger a repair, and harness runs make no forge-side LLM calls at all.
HarnessFailureKind = Literal["code", "infrastructure", "config"]

#: The one shipped harness in this wave.
HARNESS_NAME = "claude-code"

#: Name of the harness job in the target project's CI template
#: (``ci/templates/claude-code.gitlab-ci.yml``).
HARNESS_JOB_NAME = "forge-agent"

#: Machine-readable result line the harness job prints (legacy v0.2 contract,
#: kept in the templates for migration visibility only — the v0.3 backend
#: polls the candidate ARTIFACTS, never the trace line).
FORGE_RESULT_MARKER = "FORGE_RESULT:"

#: Machine-readable candidate line the proposal-only job prints (v0.3
#: contract); the authoritative payload travels as CI artifacts.
FORGE_CANDIDATE_MARKER = "FORGE_CANDIDATE:"

#: Artifact archive paths the proposal-only job uploads (ADR-0016 §1).
CANDIDATE_DIFF_PATH = ".forge/candidate.diff"
CANDIDATE_META_PATH = ".forge/candidate.meta.json"

#: Upper bound on the job-trace tail scanned for the result line.
LOG_TAIL_CHARS = 8000

#: Job statuses that mean "keep waiting" (mirrors the CI reconciler set).
_ACTIVE_JOB_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
)

#: Trace substrings meaning the harness could not run at all (auth, quota,
#: connectivity): infrastructure, never code (ADR-0015). A18 adds the
#: lane's environment-bootstrap failure marker (forge.runs.execution_profile.
#: FORGE_BOOTSTRAP_FAILED_MARKER, lowercased in job logs): a bootstrap that
#: cannot materialize the approved execution profile is infrastructure/
#: config — never a code-repair candidate.
_HARNESS_INFRASTRUCTURE_PATTERNS: tuple[str, ...] = (
    "unauthorized",
    "invalid api key",
    "authentication_error",
    "quota",
    "rate limit",
    "rate_limit",
    "credit balance",
    "billing",
    "permission denied",
    "could not resolve host",
    "connection refused",
    "forge_bootstrap_failed",
)


def is_harness_backend(name: str | None) -> bool:
    """True when *name* selects a ci_harness backend (``ci_harness[:name]``)."""
    if not name:
        return False
    return name.split(":", 1)[0].strip() == "ci_harness"


@dataclass(frozen=True)
class HarnessOutcome:
    """What one :meth:`ImplementerBackend.poll` pass learned.

    Exactly one of the constructors is meaningful per instance:

    - :meth:`change_candidate` — the backend downloaded a well-formed
      candidate bundle; the trusted publisher decides whether it becomes a
      commit (ADR-0016).
    - :meth:`change_ready` — legacy shape (a verified commit sha, builtin
      path).
    - :meth:`running` — keep waiting (durable deadline decides the rest).
    - :meth:`failed` — terminal for the harness leg; *failure_kind* says
      whether the code or the environment failed. A failed attempt that
      still published a readable usage receipt carries it in *usage* (R23):
      the lane burned the tokens whether or not the candidate was adopted,
      and the control plane ingests the partial receipt exactly once.
    """

    status: Literal["change_ready", "change_candidate", "running", "failed"]
    commit_sha: str | None = None
    bundle: CandidateBundle | None = None
    summary: str = ""
    failure_kind: HarnessFailureKind | None = None
    reason: str = ""
    #: The failed attempt's partial usage receipt, when its meta was
    #: readable and passed validation — ``None`` when nothing trustworthy
    #: was published (spend stays unknown, never invented).
    usage: HarnessUsage | None = None

    @classmethod
    def change_ready(cls, commit_sha: str, summary: str = "") -> HarnessOutcome:
        return cls(status="change_ready", commit_sha=commit_sha, summary=summary)

    @classmethod
    def change_candidate(cls, bundle: CandidateBundle, summary: str = "") -> HarnessOutcome:
        return cls(status="change_candidate", bundle=bundle, summary=summary)

    @classmethod
    def running(cls) -> HarnessOutcome:
        return cls(status="running")

    @classmethod
    def failed(
        cls,
        kind: HarnessFailureKind,
        reason: str,
        *,
        usage: HarnessUsage | None = None,
    ) -> HarnessOutcome:
        return cls(status="failed", failure_kind=kind, reason=reason, usage=usage)

    @property
    def ok(self) -> bool:
        return self.status in ("change_ready", "change_candidate")


class ImplementerBackend(Protocol):
    """The ADR-0015 backend seam. Handles are opaque JSON strings, journaled
    by the caller (in ``flow_runs.evidence``) so a run survives restarts."""

    async def start(self, run: FlowRun, issue_title: str, issue_description: str, plan: str) -> str:
        """Start implementing; return a durable handle id."""
        ...

    async def poll(self, run: FlowRun, handle: str) -> HarnessOutcome:
        """One poll pass over a previously started backend."""
        ...


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------


async def fetch_git_base(
    gitlab: GitLabClient,
    project_id: int,
    paths: list[str],
    base_sha: str | None,
) -> dict[str, str]:
    """Fetch base content for *paths* at the pinned base snapshot (ADR-0001/0006).

    Trusted layer's own read: validation is checked against the pinned
    snapshot, not against the proposer's claim. Missing files are absent
    from the result.
    """
    ref = base_sha or "HEAD"
    git_base: dict[str, str] = {}
    for path in dict.fromkeys(paths):
        try:
            repo_file = await gitlab.get_file(project_id, path, ref=ref)
        except GitLabAPIError:
            continue  # not in the snapshot — validate_changeset reports it
        content = repo_file.content
        if (repo_file.encoding or "") == "base64":
            content = base64.b64decode(content).decode("utf-8", errors="replace")
        git_base[path] = content
    return git_base


class _ArtifactMissing(Exception):
    """The candidate artifacts are absent/unreadable (404 or malformed)."""


def _plan_evidence(run: FlowRun) -> tuple[str, list[str]]:
    """Read plan summary + files_hint out of the run's evidence blob."""
    plan = (run.evidence or {}).get("plan") or {}
    summary = str(plan.get("summary") or "")
    hints = [str(hint) for hint in (plan.get("files_hint") or [])]
    return summary, hints


# ----------------------------------------------------------------------
# Builtin backend (ADR-0001 path behind the protocol)
# ----------------------------------------------------------------------


class BuiltinBackend:
    """The ``builtin`` backend: LLM → ChangeSet → publisher (ADR-0001/0016).

    Wraps the propose path ``RunService`` drives synchronously for builtin
    runs. Since ADR-0016 the produced ChangeSet is wrapped as a
    :class:`CandidateBundle` and published through the SAME trusted boundary
    as harness candidates (grant, spec digest, policy validation, journaled
    write pinned to the attempt base).
    """

    def __init__(
        self,
        *,
        implementer: Any,
        gitlab: GitLabClient,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Any,
        writer_class: type[ChangesetWriter] = ChangesetWriter,
    ) -> None:
        self._implementer = implementer
        self._gitlab = gitlab
        self._session_factory = session_factory
        self._settings = settings
        self._writer_class = writer_class

    async def start(self, run: FlowRun, issue_title: str, issue_description: str, plan: str) -> str:
        plan_summary, files_hint = _plan_evidence(run)
        changeset = await self._implementer.propose(
            run,
            issue_title,
            plan_summary=plan_summary,
            files_hint=files_hint,
        )
        attempt_base = attempt_base_for(run)
        bundle = bundle_from_changeset(changeset, attempt_base_oid=attempt_base)
        writer = self._writer_class(self._gitlab, self._session_factory, run.project_id)
        result = await publish_candidate(
            gitlab=self._gitlab,
            session_factory=self._session_factory,
            writer=writer,
            run=run,
            bundle=bundle,
            commit_message=changeset.commit_message,
        )
        if not result.ok:
            return json.dumps({"error": result.reason})
        return json.dumps({"commit_sha": result.commit_sha, "branch": changeset.branch})

    async def poll(self, run: FlowRun, handle: str) -> HarnessOutcome:
        data = json.loads(handle)
        if "error" in data:
            return HarnessOutcome.failed("code", str(data["error"]))
        return HarnessOutcome.change_ready(str(data["commit_sha"]), "builtin changeset committed")


# ----------------------------------------------------------------------
# CI harness backend (ADR-0015: claude-code in the target project's CI)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class BackendStartSpec:
    """The approved execution shape a harness backend must execute (C05).

    Built from the digest-verified RunSpec at every dispatch (initial,
    repair, retry, recovery) — the adapter NEVER re-resolves model/target
    from live settings: a post-approval settings change or a worker
    restart cannot move what the gate approved.
    """

    model: str
    target_branch: str
    driver: str
    attempt_base: str
    timeout_seconds: int = 0


class CITharnessBackend:
    """Delegates implementation to a harness job in the target project's CI.

    ``start`` ensures the factory branch exists (reusing the writer's
    branch logic), triggers a pipeline on it with the task brief and the
    FROZEN attempt base (``FORGE_ATTEMPT_BASE``, ADR-0016 §4) as pipeline
    variables, and journals pipeline + job ids into the handle.

    ``poll`` maps the job status onto a :class:`HarnessOutcome`:

    - active job → ``running`` (until the run-level durable deadline);
    - success → download the candidate artifacts, parse the diff into a
      :class:`CandidateBundle` and return ``change_candidate`` — the trusted
      publisher then decides (ADR-0016). The lane cannot push, so there is
      no branch head to verify; the diff base and the attempt base ARE the
      verification surface;
    - failed → classify (auth/quota/runner patterns and ``failure_reason``
      mean infrastructure; otherwise code).
    """

    def __init__(
        self,
        *,
        gitlab: GitLabClient,
        writer: Any,
        settings: Any,
        harness: str = HARNESS_NAME,
    ) -> None:
        self._gitlab = gitlab
        self._writer = writer  # ChangesetWriter-like: ensure_branch(branch, ref)
        self._settings = settings
        self._harness = harness

    # -- start ------------------------------------------------------------

    async def start(
        self,
        run: FlowRun,
        issue_title: str,
        issue_description: str,
        plan: str,
        spec: BackendStartSpec | None = None,
    ) -> str:
        branch = factory_branch(run.issue_iid, run.id)
        # C05: the APPROVED shape wins over live defaults — the caller
        # passes a BackendStartSpec built from the frozen RunSpec; only a
        # legacy caller without one falls back to settings (loudly).
        start_ref = (
            spec.target_branch
            if spec
            else (getattr(self._settings, "FORGE_TARGET_BRANCH", "main") or "main")
        )
        await self._writer.ensure_branch(branch, start_ref)

        model = (
            spec.model if spec else str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or "")
        )
        attempt_base = attempt_base_for(run)
        variables = [
            {"key": "FORGE_RUN_ID", "value": run.id},
            {"key": "FORGE_ISSUE_IID", "value": str(run.issue_iid or 0)},
            {"key": "FORGE_ISSUE_TITLE", "value": issue_title},
            {"key": "FORGE_PLAN", "value": plan},
            {"key": "FORGE_HARNESS_MODEL", "value": model},
            # ADR-0023 §7: the per-template rules filter — a repo including
            # MULTIPLE forge templates runs exactly one lane (this one).
            {"key": "FORGE_HARNESS_DRIVER", "value": self._harness},
            # ADR-0016 §4: the lane works on the FROZEN attempt base —
            # cycle 1 builds on the approved source base, a repair on the
            # last verified candidate. No write credential is needed.
            {"key": "FORGE_ATTEMPT_BASE", "value": attempt_base},
        ]
        pipeline = await self._gitlab.create_pipeline(run.project_id, branch, variables=variables)
        pipeline_id = int(pipeline["id"])
        job_id = await self._discover_job_id(run.project_id, pipeline_id)

        handle = json.dumps(
            {
                "harness": self._harness,
                "pipeline_id": pipeline_id,
                "job_id": job_id,
                "branch": branch,
                "base_sha": run.base_sha or "",
                "attempt_base": attempt_base,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info(
            "Harness %s started for run %s (pipeline %d, job %s, base %s)",
            self._harness,
            run.id[:8],
            pipeline_id,
            job_id,
            attempt_base[:8],
        )
        return handle

    async def _discover_job_id(self, project_id: int, pipeline_id: int) -> int | None:
        """Find the harness job in the pipeline; None → re-discover in poll."""
        try:
            jobs = await self._gitlab.list_pipeline_jobs(project_id, pipeline_id)
        except GitLabAPIError:
            return None
        for job in jobs:
            if job.name == HARNESS_JOB_NAME:
                return job.id
        return None

    # -- poll -------------------------------------------------------------

    async def poll(
        self,
        run: FlowRun,
        handle: str,
        *,
        now: datetime | None = None,
    ) -> HarnessOutcome:
        data = json.loads(handle)
        project_id = run.project_id
        pipeline_id = int(data["pipeline_id"])
        now = now or datetime.now(timezone.utc)

        job = await self._find_job(project_id, pipeline_id, data)
        if job is None:
            # The job list is not usable (empty / read failed): still enforce
            # the durable deadline so the run cannot wait forever.
            return self._deadline_outcome(data, now)

        if (job.status or "").lower() in _ACTIVE_JOB_STATUSES:
            return self._deadline_outcome(data, now)

        if (job.status or "").lower() == "success":
            return await self._collect_candidate(project_id, job, run)

        if (job.status or "").lower() in {"canceled", "cancelled", "skipped"}:
            # External cancellation is not the code's fault (ADR-0015).
            return HarnessOutcome.failed("infrastructure", f"harness job {job.name} canceled")

        return await self._classify_failure(project_id, pipeline_id, job)

    async def _find_job(self, project_id: int, pipeline_id: int, data: dict[str, Any]):
        """Locate the harness job, re-discovering by name when needed."""
        try:
            jobs = await self._gitlab.list_pipeline_jobs(project_id, pipeline_id)
        except GitLabAPIError:
            logger.warning(
                "Job read failed for harness pipeline %s — keeping the run waiting",
                pipeline_id,
                exc_info=True,
            )
            return None
        wanted_id = data.get("job_id")
        for job in jobs:
            if job.id == wanted_id or job.name == HARNESS_JOB_NAME:
                return job
        return None

    def _deadline_outcome(self, data: dict[str, Any], now: datetime) -> HarnessOutcome:
        """running before the durable deadline; harness_timeout past it.

        The deadline derives from the journaled evidence timestamp (the
        handle's ``started_at``) plus ``FORGE_HARNESS_TIMEOUT_SECONDS``
        (ADR-0013 budgets enforced by the controller, not the harness).
        """
        timeout = int(getattr(self._settings, "FORGE_HARNESS_TIMEOUT_SECONDS", 1800) or 1800)
        started_at = data.get("started_at")
        if started_at:
            try:
                started = datetime.fromisoformat(str(started_at))
            except ValueError:
                started = None
            if started is not None and as_aware_utc(now) > as_aware_utc(started) + timedelta(
                seconds=timeout
            ):
                return HarnessOutcome.failed("infrastructure", "harness_timeout")
        return HarnessOutcome.running()

    async def _collect_candidate(self, project_id: int, job, run: FlowRun) -> HarnessOutcome:
        """Download the candidate artifacts and build the bundle (ADR-0016).

        The attempt base compared against the artifact's meta is computed
        from the TRUSTED run state (never from the artifact alone); the
        publisher re-checks it at the write boundary.
        """
        attempt_base = attempt_base_for(run)

        try:
            meta = await self._download_meta(project_id, job.id)
        except _ArtifactMissing:
            return HarnessOutcome.failed("code", "harness_artifact_missing")
        reported_base = str(meta.get("attempt_base") or "")
        if reported_base != attempt_base:
            return HarnessOutcome.failed(
                "code",
                "harness_attempt_base_mismatch: artifact base "
                f"{reported_base[:8] or '<none>'}, expected {attempt_base[:8] or '<none>'}",
            )

        driver_exit = str(meta.get("exit") or "completed").strip() or "completed"
        usage = HarnessUsage.from_meta(
            meta.get("usage"),
            driver=str(meta.get("driver") or self._harness),
            model=str(meta.get("model") or ""),
        )

        try:
            diff_bytes = await self._gitlab.get_job_artifacts_file(
                project_id, job.id, CANDIDATE_DIFF_PATH
            )
        except GitLabAPIError as exc:
            if exc.status_code == 404:
                return HarnessOutcome.failed("code", "harness_artifact_missing")
            raise
        diff_text = diff_bytes.decode("utf-8", errors="replace")

        try:
            bundle = parse_unified_diff(
                diff_text,
                attempt_base_oid=attempt_base,
                driver_exit=driver_exit,
                usage=usage,
            )
        except CandidateError as exc:
            return HarnessOutcome.failed("code", f"harness_candidate_invalid: {exc.reason}: {exc}")

        if driver_exit != "completed":
            # The driver itself reported failure: never adopt a possibly
            # partial working tree, whatever it managed to change.
            detail = " with no changes" if bundle.is_empty else ""
            return HarnessOutcome.failed(
                "code", f"harness_driver_failed (exit={driver_exit}{detail})"
            )
        if bundle.is_empty:
            # F20: a repair that changes nothing is "no effect", not a
            # no-op adoption; cycle 1 with an empty diff is plain no-change.
            if (run.commit_cycle or 1) > 1:
                return HarnessOutcome.failed("code", "repair_no_effect")
            return HarnessOutcome.failed("code", "harness_no_changes")
        return HarnessOutcome.change_candidate(bundle, summary=str(meta.get("summary") or ""))

    async def _download_meta(self, project_id: int, job_id: int) -> dict[str, Any]:
        """Fetch + parse ``candidate.meta.json``; raises ``_ArtifactMissing``."""
        try:
            raw = await self._gitlab.get_job_artifacts_file(project_id, job_id, CANDIDATE_META_PATH)
        except GitLabAPIError as exc:
            if exc.status_code == 404:
                raise _ArtifactMissing() from exc
            raise
        try:
            meta = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _ArtifactMissing() from exc
        if not isinstance(meta, dict):
            raise _ArtifactMissing()
        return meta

    async def _classify_failure(
        self,
        project_id: int,
        pipeline_id: int,
        job,
    ) -> HarnessOutcome:
        """Map a failed harness job onto a failure kind (ADR-0015).

        Auth/quota/connectivity patterns in the trace mean the *environment*
        failed → infrastructure; otherwise the job's ``failure_reason``
        classifies it (infrastructure > config > code, ADR-0008). An empty
        failure reason is treated as infrastructure too: "unknown" means the
        evidence does not blame the code (seen live: a canceled job read
        transiently as failed with no reason).
        """
        try:
            log = await self._gitlab.get_job_log(project_id, job.id, tail=LOG_TAIL_CHARS)
        except GitLabAPIError:
            log = ""
        lowered = log.lower()
        reason = (job.failure_reason or "").strip().lower()
        detail = f"harness job {job.name} failed ({reason or 'unknown reason'})"
        if not reason or any(pattern in lowered for pattern in _HARNESS_INFRASTRUCTURE_PATTERNS):
            return HarnessOutcome.failed("infrastructure", detail)

        try:
            jobs = await self._gitlab.list_pipeline_jobs(project_id, pipeline_id)
        except GitLabAPIError:
            jobs = []
        kind = classify_failure(jobs) if jobs else "code"
        if kind not in ("code", "infrastructure", "config"):
            kind = "code"
        # The clamp above guarantees the value is a HarnessFailureKind.
        return HarnessOutcome.failed(cast(HarnessFailureKind, kind), detail)


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------


def build_backend(
    settings: Any,
    *,
    gitlab: GitLabClient,
    session_factory: async_sessionmaker[AsyncSession],
    writer: Any | None = None,
    implementer: Any | None = None,
    driver: str | None = None,
) -> ImplementerBackend:
    """Construct the configured implementer backend (``FORGE_IMPLEMENTER_BACKEND``).

    Values: ``builtin`` (default) or ``ci_harness[:<harness>]`` — the harness
    name defaults to ``claude-code``. *driver* (ADR-0023) overrides the
    suffix-parsed harness: the dispatch leg runs the driver frozen in the
    RunSpec, not whatever the setting says post-gate. Unknown values raise
    ``ValueError`` (a configuration error, not a runtime condition).
    """
    raw = str(getattr(settings, "FORGE_IMPLEMENTER_BACKEND", "builtin") or "builtin").strip()
    kind, _, suffix = raw.partition(":")
    harness = suffix.strip() if suffix.strip() else HARNESS_NAME
    # ADR-0023: an explicit driver (the RunSpec's frozen selection) wins —
    # the dispatch leg runs the approved driver, never a live-setting one.
    if driver and driver.strip():
        harness = driver.strip()

    if kind == "builtin":
        if implementer is None:
            raise ValueError("builtin backend requires an implementer agent")
        return BuiltinBackend(
            implementer=implementer,
            gitlab=gitlab,
            session_factory=session_factory,
            settings=settings,
        )
    if kind == "ci_harness":
        if writer is None:
            raise ValueError("ci_harness backend requires a ChangesetWriter")
        return CITharnessBackend(
            gitlab=gitlab,
            writer=writer,
            settings=settings,
            harness=harness,
        )
    raise ValueError(f"unknown FORGE_IMPLEMENTER_BACKEND {kind!r} (expected builtin | ci_harness)")
