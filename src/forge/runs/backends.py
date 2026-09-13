"""Pluggable implementer backends (ADR-0015).

The implementer step of a run is pluggable; the controller owns the lifecycle
unchanged (ADR-0004). Two backends:

- ``BuiltinBackend`` — today's LLM → ChangeSet → Commits API path (ADR-0001).
  ``RunService`` keeps driving this path synchronously for builtin runs; this
  adapter exposes the same propose→validate→materialize→commit flow behind
  the backend protocol.
- ``CITharnessBackend`` — delegates implementation to a coding harness
  (``claude-code``) executing as a job in the *target project's* CI
  (never on forge infrastructure, ADR-0002/0015). The job is triggered as a
  pipeline on the factory branch; forge polls it and adopts the result only
  after verification against the real branch head — the harness's own
  success claim is never trusted.

Trust boundary (ADR-0015): the verified branch head SHA — read back from
GitLab, equal to the harness's reported head and different from the base —
is the only thing that may become a candidate commit. Harness-level failures
(auth, quota, runner, timeout) classify as ``infrastructure``, never ``code``.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from forge.durable import FlowRun, as_aware_utc
from forge.durable.identity import factory_branch
from forge.gitlab.client import GitLabAPIError, GitLabClient
from forge.repository import ChangesetWriter, WriteOutcome, validate_changeset
from forge.runs.ci_contract import classify_failure

logger = logging.getLogger(__name__)

#: Harness failure kinds (ADR-0015): infrastructure/config failures never
#: trigger a repair, and harness runs make no forge-side LLM calls at all.
HarnessFailureKind = Literal["code", "infrastructure", "config"]

#: The one shipped harness in this wave.
HARNESS_NAME = "claude-code"

#: Name of the harness job in the target project's CI template
#: (``ci/templates/claude-code.gitlab-ci.yml``).
HARNESS_JOB_NAME = "forge-agent"

#: Machine-readable result line the harness job prints last.
FORGE_RESULT_MARKER = "FORGE_RESULT:"

#: Upper bound on the job-trace tail scanned for the result line.
LOG_TAIL_CHARS = 8000

#: Job statuses that mean "keep waiting" (mirrors the CI reconciler set).
_ACTIVE_JOB_STATUSES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
)

#: Trace substrings meaning the harness could not run at all (auth, quota,
#: connectivity): infrastructure, never code (ADR-0015).
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

    - :meth:`change_ready` — the backend verified a real change; adopt it.
    - :meth:`running` — keep waiting (durable deadline decides the rest).
    - :meth:`failed` — terminal for the harness leg; *failure_kind* says
      whether the code or the environment failed.
    """

    status: Literal["change_ready", "running", "failed"]
    commit_sha: str | None = None
    summary: str = ""
    failure_kind: HarnessFailureKind | None = None
    reason: str = ""

    @classmethod
    def change_ready(cls, commit_sha: str, summary: str = "") -> HarnessOutcome:
        return cls(status="change_ready", commit_sha=commit_sha, summary=summary)

    @classmethod
    def running(cls) -> HarnessOutcome:
        return cls(status="running")

    @classmethod
    def failed(cls, kind: HarnessFailureKind, reason: str) -> HarnessOutcome:
        return cls(status="failed", failure_kind=kind, reason=reason)

    @property
    def ok(self) -> bool:
        return self.status == "change_ready"


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


def _plan_evidence(run: FlowRun) -> tuple[str, list[str]]:
    """Read plan summary + files_hint out of the run's evidence blob."""
    plan = (run.evidence or {}).get("plan") or {}
    summary = str(plan.get("summary") or "")
    hints = [str(hint) for hint in (plan.get("files_hint") or [])]
    return summary, hints


def _parse_forge_result(log: str) -> dict[str, Any] | None:
    """Extract the last ``FORGE_RESULT:{...}`` JSON object from a job trace."""
    for line in reversed(log.splitlines()):
        idx = line.find(FORGE_RESULT_MARKER)
        if idx == -1:
            continue
        raw = line[idx + len(FORGE_RESULT_MARKER) :].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ----------------------------------------------------------------------
# Builtin backend (ADR-0001 path behind the protocol)
# ----------------------------------------------------------------------


class BuiltinBackend:
    """The ``builtin`` backend: LLM → ChangeSet → Commits API (ADR-0001).

    Wraps the same propose → validate → materialize → commit path
    ``RunService`` drives synchronously for builtin runs, expressed in the
    start/poll protocol. ``start`` runs the whole path; ``poll`` reports the
    journaled result.
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
        git_base = await fetch_git_base(
            self._gitlab,
            run.project_id,
            [change.path for change in changeset.changes],
            run.base_sha,
        )
        violations = validate_changeset(changeset, git_base)
        if violations:
            return json.dumps({"error": "changeset_invalid: " + "; ".join(violations)})

        writer = self._writer_class(self._gitlab, self._session_factory, run.project_id)
        result = await writer.apply(
            run.id, changeset, start_ref=getattr(self._settings, "FORGE_TARGET_BRANCH", "main")
        )
        if result.outcome is WriteOutcome.UNKNOWN or not result.commit_sha:
            return json.dumps({"error": "commit_unknown_outcome"})
        return json.dumps({"commit_sha": result.commit_sha, "branch": changeset.branch})

    async def poll(self, run: FlowRun, handle: str) -> HarnessOutcome:
        data = json.loads(handle)
        if "error" in data:
            return HarnessOutcome.failed("code", str(data["error"]))
        return HarnessOutcome.change_ready(str(data["commit_sha"]), "builtin changeset committed")


# ----------------------------------------------------------------------
# CI harness backend (ADR-0015: claude-code in the target project's CI)
# ----------------------------------------------------------------------


class CITharnessBackend:
    """Delegates implementation to a harness job in the target project's CI.

    ``start`` ensures the factory branch exists (reusing the writer's
    branch logic), triggers a pipeline on it with the task brief as
    pipeline variables, and journals pipeline + job ids into the handle.

    ``poll`` maps the job status onto a :class:`HarnessOutcome`:

    - active job → ``running`` (until the run-level durable deadline);
    - success → verify against the **real branch head** (never the claim):
      head must differ from the base and equal the job's reported
      ``FORGE_RESULT`` head;
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

    async def start(self, run: FlowRun, issue_title: str, issue_description: str, plan: str) -> str:
        branch = factory_branch(run.issue_iid, run.id)
        start_ref = getattr(self._settings, "FORGE_TARGET_BRANCH", "main") or "main"
        await self._writer.ensure_branch(branch, start_ref)

        model = str(getattr(self._settings, "FORGE_HARNESS_MODEL", "") or "")
        variables = [
            {"key": "FORGE_RUN_ID", "value": run.id},
            {"key": "FORGE_ISSUE_IID", "value": str(run.issue_iid or 0)},
            {"key": "FORGE_ISSUE_TITLE", "value": issue_title},
            {"key": "FORGE_PLAN", "value": plan},
            {"key": "FORGE_HARNESS_MODEL", "value": model},
        ]
        pipeline = await self._gitlab.create_pipeline(run.project_id, branch, variables=variables)
        pipeline_id = int(pipeline.get("id"))
        job_id = await self._discover_job_id(run.project_id, pipeline_id)

        handle = json.dumps(
            {
                "harness": self._harness,
                "pipeline_id": pipeline_id,
                "job_id": job_id,
                "branch": branch,
                "base_sha": run.base_sha or "",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.info(
            "Harness %s started for run %s (pipeline %d, job %s)",
            self._harness,
            run.id[:8],
            pipeline_id,
            job_id,
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
        branch = str(data["branch"])
        base_sha = str(data.get("base_sha") or run.base_sha or "")
        now = now or datetime.now(timezone.utc)

        job = await self._find_job(project_id, pipeline_id, data)
        if job is None:
            # The job list is not usable (empty / read failed): still enforce
            # the durable deadline so the run cannot wait forever.
            return self._deadline_outcome(data, now)

        if (job.status or "").lower() in _ACTIVE_JOB_STATUSES:
            return self._deadline_outcome(data, now)

        if (job.status or "").lower() == "success":
            return await self._verify(project_id, job, branch, base_sha)

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

    async def _verify(
        self,
        project_id: int,
        job,
        branch: str,
        base_sha: str,
    ) -> HarnessOutcome:
        """Adopt the harness result only after SHA verification (ADR-0015)."""
        try:
            branch_info = await self._gitlab.get_branch(project_id, branch)
        except GitLabAPIError as exc:
            if exc.status_code == 404:
                return HarnessOutcome.failed("code", "harness_no_changes")
            raise
        head = str(((branch_info.get("commit") or {}).get("id")) or "")
        if not head or head == base_sha:
            return HarnessOutcome.failed("code", "harness_no_changes")

        try:
            log = await self._gitlab.get_job_log(project_id, job.id, tail=LOG_TAIL_CHARS)
        except GitLabAPIError:
            log = ""
        reported = _parse_forge_result(log)
        if reported is None:
            return HarnessOutcome.failed("code", "harness_result_missing")

        reported_head = str(reported.get("head") or "")
        if reported_head != head:
            return HarnessOutcome.failed(
                "code",
                f"harness_sha_mismatch: reported {reported_head[:8] or '<none>'}, "
                f"actual branch head {head[:8]}",
            )
        return HarnessOutcome.change_ready(head, str(reported.get("summary") or ""))

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
        return HarnessOutcome.failed(kind, detail)


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
) -> ImplementerBackend:
    """Construct the configured implementer backend (``FORGE_IMPLEMENTER_BACKEND``).

    Values: ``builtin`` (default) or ``ci_harness[:<harness>]`` — the harness
    name defaults to ``claude-code``. Unknown values raise ``ValueError``
    (a configuration error, not a runtime condition).
    """
    raw = str(getattr(settings, "FORGE_IMPLEMENTER_BACKEND", "builtin") or "builtin").strip()
    harness = HARNESS_NAME
    if ":" in raw:
        raw, _, suffix = raw.partition(":")
        if suffix.strip():
            harness = suffix.strip()

    if raw == "builtin":
        if implementer is None:
            raise ValueError("builtin backend requires an implementer agent")
        return BuiltinBackend(
            implementer=implementer,
            gitlab=gitlab,
            session_factory=session_factory,
            settings=settings,
        )
    if raw == "ci_harness":
        if writer is None:
            raise ValueError("ci_harness backend requires a ChangesetWriter")
        return CITharnessBackend(
            gitlab=gitlab,
            writer=writer,
            settings=settings,
            harness=harness,
        )
    raise ValueError(f"unknown FORGE_IMPLEMENTER_BACKEND {raw!r} (expected builtin | ci_harness)")
