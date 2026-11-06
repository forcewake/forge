"""Actions attempts discovered by exact durable dispatch and workflow identity."""

from collections.abc import Awaitable, Callable, Collection
from typing import Literal
from urllib.parse import quote, urlsplit

from forge_protocol import LaneDispatch

from forge.port_values import (
    Applied,
    CheckResult,
    DispatchOutcome,
    Found,
    Inconclusive,
    LaneHandle,
    LaneStatus,
    ProbeResult,
    ReadUnavailable,
    RepoRef,
)
from forge.ports import Clock
from forge.providers.github.transport import TransportFailure
from forge.providers.github.client import API
from forge.providers.github.repo import (
    evidence,
    list_doc,
    malformed,
    native_id,
    object_doc,
    object_sha,
    repo_path,
    request,
    text,
)

from forge.verification.failure_proof import FailureProof, finished, junit_failure, trusted_jobs

DispatchLookup = Callable[[RepoRef, str], Awaitable[LaneDispatch | None]]


def run_title(dispatch: LaneDispatch) -> str:
    if dispatch.nonce is None:
        raise malformed("missing_dispatch_nonce")
    return f"forge-lane:{dispatch.forge_run_id}:{dispatch.attempt}:{dispatch.generation}:{dispatch.nonce}"


def workflow_path(value: object) -> str:
    path = text(value).split("@", 1)[0]
    if (
        not path.startswith(".github/workflows/")
        or path.count("/") != 2
        or not path.endswith((".yml", ".yaml"))
    ):
        raise malformed("invalid_workflow_path")
    return repo_path(path)


def job_status(
    row: dict[str, object],
) -> Literal["pending", "passed", "failed", "cancelled", "skipped", "unknown"]:
    if row.get("status") in ("queued", "in_progress", "requested", "waiting", "pending"):
        return "pending"
    if row.get("status") != "completed":
        return "unknown"
    conclusion = row.get("conclusion")
    if conclusion == "success":
        return "passed"
    if conclusion in ("failure", "timed_out", "action_required", "startup_failure", "stale"):
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    if conclusion in ("skipped", "neutral"):
        return "skipped"
    return "unknown"


def failure_kind(row: dict[str, object]) -> Literal["infrastructure", "configuration", "unknown"]:
    conclusion = row.get("conclusion")
    if conclusion in ("timed_out", "cancelled", "startup_failure", "stale"):
        return "infrastructure"
    if conclusion == "action_required":
        return "configuration"
    # A generic failure can be checkout/setup/network failure. It is not a
    # typed code defect and cannot authorize another paid implementation.
    return "unknown"


class GitHubCI:
    def __init__(
        self,
        api: API,
        clock: Clock,
        *,
        dispatch_lookup: DispatchLookup,
        lane_workflow_path: str,
        verification_workflow_paths: Collection[str],
        verification_failure_jobs: Collection[str] = (),
    ) -> None:
        if isinstance(verification_workflow_paths, (str, bytes)):
            raise ValueError("workflow paths must be an explicit collection")
        self.api, self.clock, self.dispatch_lookup = api, clock, dispatch_lookup
        self.failure_jobs = trusted_jobs(verification_failure_jobs)
        self.lane_workflow_path = workflow_path(lane_workflow_path)
        self.verification_paths = frozenset(
            workflow_path(path) for path in verification_workflow_paths
        )
        if self.lane_workflow_path in self.verification_paths:
            raise ValueError("lane workflow cannot be independent verification")

    async def workflow(self, repo: RepoRef) -> dict[str, object]:
        filename = self.lane_workflow_path.rsplit("/", 1)[1]
        row = object_doc(
            (
                await request(self.api, repo, "actions/workflows/" + quote(filename, safe=""))
            ).document
        )
        native_id(row.get("id"))
        if row.get("path") != self.lane_workflow_path or row.get("state") != "active":
            raise malformed("lane_workflow_identity_mismatch")
        return row

    async def _pages(
        self, repo: RepoRef, resource: str, field: str, params: dict[str, str | int] | None = None
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for page in range(1, 11):
            doc = object_doc(
                (
                    await request(
                        self.api,
                        repo,
                        resource,
                        params={**(params or {}), "per_page": 100, "page": page},
                    )
                ).document
            )
            total = doc.get("total_count")
            if type(total) is not int or not 0 <= total <= 1000:
                raise malformed("actions_scan_unbounded")
            batch = list_doc(doc.get(field))
            if len(batch) > 100:
                raise malformed("actions_page_too_large")
            for raw in batch:
                row = object_doc(raw)
                ident = native_id(row.get("id"))
                if ident in seen:
                    raise malformed("duplicate_actions_page")
                seen.add(ident)
                rows.append(row)
            if len(batch) < 100:
                if len(rows) != total:
                    raise malformed("actions_scan_changed")
                return rows
        raise malformed("actions_scan_truncated")

    async def binding(self, repo: RepoRef, nonce: str) -> LaneDispatch:
        value = await self.dispatch_lookup(repo, nonce)
        if value is None or value.nonce != nonce or value.provider != "github":
            raise malformed("durable_dispatch_not_visible")
        # Name is descriptive; immutable native repository authority comes from the lookup key.
        if not value.ref.startswith("forge/"):
            raise malformed("dispatch_branch_not_factory_owned")
        return value

    def _run(
        self, repo: RepoRef, row: dict[str, object], dispatch: LaneDispatch, workflow_id: str
    ) -> LaneHandle:
        if (
            native_id(object_doc(row.get("repository")).get("id")) != repo.identity.repository_id
            or native_id(object_doc(row.get("head_repository")).get("id"))
            != repo.identity.repository_id
            or native_id(row.get("workflow_id")) != workflow_id
            or workflow_path(row.get("path")) != self.lane_workflow_path
            or row.get("event") != "workflow_dispatch"
            or row.get("head_branch") != dispatch.ref
            or row.get("head_sha") != dispatch.execution_base_sha
            or row.get("display_title") != run_title(dispatch)
        ):
            raise malformed("native_dispatch_binding_mismatch")
        if dispatch.nonce is None:
            raise malformed("missing_dispatch_nonce")
        return LaneHandle(
            repo=repo,
            native_id=native_id(row.get("id")),
            attempt=int(native_id(row.get("run_attempt"))),
            correlation_id=dispatch.nonce,
        )

    async def probe_dispatch(
        self, repo: RepoRef, correlation_id: str
    ) -> ProbeResult[DispatchOutcome]:
        try:
            dispatch = await self.binding(repo, correlation_id)
            workflow_id = native_id((await self.workflow(repo)).get("id"))
            rows = await self._pages(
                repo,
                "actions/workflows/" + workflow_id + "/runs",
                "workflow_runs",
                {
                    "event": "workflow_dispatch",
                    "branch": dispatch.ref,
                    "head_sha": dispatch.execution_base_sha,
                },
            )
            matches = [row for row in rows if row.get("display_title") == run_title(dispatch)]
            if len(matches) != 1:
                return Inconclusive(
                    reason="dispatch nonce missing or ambiguous",
                    evidence=evidence("probe_dispatch"),
                )
            handle = self._run(repo, matches[0], dispatch, workflow_id)
            proof = evidence(
                "probe_dispatch", native_id=handle.native_id, attempt=str(handle.attempt)
            )
            return Found[DispatchOutcome](
                value=Applied[LaneHandle](value=handle, evidence=proof), evidence=proof
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="native dispatch read unavailable", evidence=error.evidence)

    async def find_lane(self, repo: RepoRef, correlation_id: str) -> LaneHandle | None:
        result = await self.probe_dispatch(repo, correlation_id)
        if isinstance(result, Found) and isinstance(result.value, Applied):
            return result.value.value
        raise ReadUnavailable("lane visibility unknown", result.evidence)

    async def lane_status(self, handle: LaneHandle) -> LaneStatus:
        dispatch = await self.binding(handle.repo, handle.correlation_id)
        workflow_id = native_id((await self.workflow(handle.repo)).get("id"))
        resource = f"actions/runs/{handle.native_id}/attempts/{handle.attempt}"
        row = object_doc((await request(self.api, handle.repo, resource)).document)
        if self._run(handle.repo, row, dispatch, workflow_id) != handle:
            raise malformed("lane_attempt_changed")
        state = job_status(row)
        mapping: dict[
            str, Literal["queued", "running", "succeeded", "failed", "cancelled", "unknown"]
        ] = {
            "passed": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "skipped": "failed",
            "unknown": "unknown",
        }
        mapped = mapping.get(state, "running" if row.get("status") == "in_progress" else "queued")
        return LaneStatus(
            handle=handle,
            status=mapped,
            observed_at=self.clock.now(),
            evidence=evidence(
                "lane_status", native_id=handle.native_id, attempt=str(handle.attempt)
            ),
        )

    async def checks_for(self, repo: RepoRef, sha: str) -> tuple[CheckResult, ...]:
        sha = object_sha(sha)
        rows = await self._pages(repo, "actions/runs", "workflow_runs", {"head_sha": sha})
        checks = []
        for row in rows:
            path = workflow_path(row.get("path"))
            if path not in self.verification_paths and path != self.lane_workflow_path:
                continue
            if (
                row.get("head_sha") != sha
                or native_id(object_doc(row.get("repository")).get("id"))
                != repo.identity.repository_id
                or native_id(object_doc(row.get("head_repository")).get("id"))
                != repo.identity.repository_id
            ):
                raise malformed("check_run_repository_or_sha_mismatch")
            run_id, attempt = native_id(row.get("id")), native_id(row.get("run_attempt"))
            jobs = await self._pages(repo, f"actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
            for job in jobs:
                if (
                    native_id(job.get("run_id")) != run_id
                    or ("run_attempt" in job and native_id(job["run_attempt"]) != attempt)
                    or job.get("head_sha") != sha
                ):
                    raise malformed("check_job_attempt_mismatch")
                proof = (
                    await self._code_proof(repo, row, job, sha)
                    if path in self.verification_paths
                    else None
                )
                checks.append(
                    CheckResult(
                        name=text(job.get("name")),
                        tested_sha=sha,
                        native_id=native_id(job.get("id")),
                        native_run_id=run_id,
                        attempt=int(attempt),
                        role="lane" if path == self.lane_workflow_path else "verification",
                        status=job_status(job),
                        observed_at=self.clock.now(),
                        workflow_path=path,
                        failure_kind="code" if proof is not None else failure_kind(job),
                        evidence=evidence(
                            "check_job",
                            native_id=native_id(job.get("id")),
                            run_id=run_id,
                            attempt=attempt,
                            **({"failure_proof": proof.encoded()} if proof else {}),
                        ),
                    )
                )
        return tuple(checks)

    async def _code_proof(
        self, repo: RepoRef, run: dict[str, object], job: dict[str, object], sha: str
    ) -> FailureProof | None:
        if (
            job.get("name") not in self.failure_jobs
            or job.get("conclusion") != "failure"
            or job.get("status") != "completed"
        ):
            return None
        job_end = finished(job.get("completed_at"), self.clock.now())
        if job_end is None:
            return None
        try:
            url = urlsplit(text(job.get("check_run_url")))
            prefix, check_id = url.path.rsplit("/check-runs/", 1)
            metadata = object_doc((await self.api.metadata(repo)).document)
            api_url = urlsplit(self.api.api_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or not check_id.isascii()
                or not check_id.isdecimal()
                or str(int(check_id)) != check_id
                or int(check_id) <= 0
                or (url.hostname, url.port or 443) != (api_url.hostname, api_url.port or 443)
                or prefix != api_url.path.rstrip("/") + "/repos/" + text(metadata.get("full_name"))
            ):
                return None
            check = object_doc((await request(self.api, repo, "check-runs/" + check_id)).document)
            suite = object_doc(check.get("check_suite"))
            app = object_doc(check.get("app"))
            check_end = finished(check.get("completed_at"), self.clock.now())
            if (
                native_id(check.get("id")) != check_id
                or check.get("head_sha") != sha
                or native_id(suite.get("id")) != native_id(run.get("check_suite_id"))
                or app.get("slug") != "github-actions"
                or not native_id(app.get("id"))
                or check.get("name") != job.get("name")
                or check.get("status") != "completed"
                or check.get("conclusion") != "failure"
                or check_end is None
                or abs((check_end - job_end).total_seconds()) > 5
            ):
                return None
            return junit_failure(object_doc(check.get("output")).get("text"))
        except (ReadUnavailable, TransportFailure, ValueError):
            return None
