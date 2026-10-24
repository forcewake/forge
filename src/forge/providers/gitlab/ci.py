"""Native pipeline/job executions with durable nonce binding and honest CI roles."""

from collections.abc import Awaitable, Callable, Collection
from typing import Literal

from forge_protocol import LaneDispatch
from pydantic import ValidationError

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
from forge.providers.gitlab.client import API
from forge.providers.gitlab.values import (
    evidence,
    native_id,
    numeric,
    obj,
    path,
    sha as object_sha,
    text,
    unavailable,
)

DispatchLookup = Callable[[RepoRef, str], Awaitable[LaneDispatch | None]]


def check_status(
    value: object,
) -> Literal["pending", "passed", "failed", "cancelled", "skipped", "unknown"]:
    if value in (
        "created",
        "pending",
        "preparing",
        "running",
        "scheduled",
        "waiting_for_resource",
        "waiting_for_callback",
    ):
        return "pending"
    if value == "success":
        return "passed"
    if value == "failed":
        return "failed"
    if value in ("canceled", "canceling"):
        return "cancelled"
    if value == "skipped":
        return "skipped"
    return "unknown"


def failure_kind(
    row: dict[str, object],
) -> Literal["code", "infrastructure", "configuration", "unknown"]:
    if row.get("status") != "failed":
        return "unknown"
    reason = row.get("failure_reason")
    if reason in (
        "api_failure",
        "runner_system_failure",
        "runner_external_dependency_failure",
        "runner_interrupted",
        "stuck_or_timeout_failure",
        "stuck_pending_with_matching_runners",
        "no_updates_running",
        "no_updates_canceling",
        "scheduler_failure",
        "job_execution_timeout",
        "server_timeout_running",
        "server_timeout_canceling",
    ):
        return "infrastructure"
    if reason in (
        "runner_configuration_error",
        "runner_unsupported",
        "stuck_pending_no_matching_runners",
        "unmet_prerequisites",
        "missing_dependency_failure",
        "config_error",
    ):
        return "configuration"
    # script_failure also covers setup/image-pull failures on supported CE
    # releases. It cannot establish a code fault without separate native proof.
    return "unknown"


class GitLabCI:
    def __init__(
        self,
        api: API,
        clock: Clock,
        *,
        dispatch_lookup: DispatchLookup,
        verification_sources: Collection[str] = ("push", "merge_request_event"),
        configuration_path: str = ".gitlab-ci.yml",
    ) -> None:
        if isinstance(verification_sources, (str, bytes)) or not set(verification_sources) <= {
            "push",
            "merge_request_event",
        }:
            raise ValueError("independent CI sources must be explicitly trusted")
        self.api, self.clock, self.dispatch_lookup = api, clock, dispatch_lookup
        self.verification_sources, self.configuration_path = (
            frozenset(verification_sources),
            path(configuration_path),
        )

    async def binding(self, repo: RepoRef, nonce: str) -> LaneDispatch:
        value = await self.dispatch_lookup(repo, nonce)
        if (
            value is None
            or value.provider != "gitlab"
            or value.nonce != nonce
            or not value.ref.startswith("forge/")
        ):
            raise unavailable("durable_dispatch_not_visible")
        return value

    async def pipeline(self, repo: RepoRef, ident: str) -> dict[str, object]:
        row = obj((await self.api.read(repo, "pipelines/" + numeric(ident))).document)
        if (
            native_id(row.get("id")) != ident
            or native_id(row.get("project_id")) != repo.identity.repository_id
        ):
            raise unavailable("pipeline_identity_mismatch")
        object_sha(row.get("sha"))
        if row.get("yaml_errors") is not None:
            raise unavailable("pipeline_configuration_errors")
        return row

    async def variables(self, repo: RepoRef, ident: str) -> dict[str, str]:
        rows = await self.api.pages(repo, "pipelines/" + numeric(ident) + "/variables")
        result = {}
        for row in rows:
            key = text(row.get("key"))
            if key in result or row.get("variable_type") not in (None, "env_var"):
                raise unavailable("ambiguous_pipeline_variables")
            result[key] = text(row.get("value"), empty=True)
        return result

    def _handle(
        self,
        repo: RepoRef,
        row: dict[str, object],
        variables: dict[str, str],
        dispatch: LaneDispatch,
    ) -> LaneHandle:
        bot = self.api.credentials(repo).bot
        user = obj(row.get("user"))
        if (
            native_id(row.get("project_id")) != repo.identity.repository_id
            or row.get("source") != "api"
            or row.get("ref") != dispatch.ref
            or row.get("sha") != dispatch.base_sha
            or variables.get("FORGE_LANE_NONCE") != dispatch.nonce
            or native_id(user.get("id")) != bot.native_id
            or user.get("username") != bot.login
        ):
            raise unavailable("pipeline_dispatch_binding_mismatch")
        try:
            exact = LaneDispatch.model_validate_json(variables.get("FORGE_LANE_DISPATCH", ""))
        except ValidationError:
            raise unavailable("invalid_pipeline_dispatch") from None
        if exact != dispatch or dispatch.nonce is None:
            raise unavailable("pipeline_dispatch_payload_mismatch")
        ident = native_id(row.get("id"))
        # GitLab has no pipeline retry ordinal: the actual immutable execution ID
        # occupies attempt as well. New Forge generations create new pipelines.
        return LaneHandle(
            repo=repo, native_id=ident, attempt=int(ident), correlation_id=dispatch.nonce
        )

    async def probe_dispatch(
        self, repo: RepoRef, correlation_id: str
    ) -> ProbeResult[DispatchOutcome]:
        try:
            dispatch = await self.binding(repo, correlation_id)
            rows = await self.api.pages(
                repo,
                "pipelines",
                params={"ref": dispatch.ref, "sha": dispatch.base_sha, "source": "api"},
            )
            matches = []
            seen: set[str] = set()
            for raw in rows:
                ident = native_id(raw.get("id"))
                if ident in seen:
                    raise unavailable("duplicate_pipeline_page")
                seen.add(ident)
                row = await self.pipeline(repo, ident)
                variables = await self.variables(repo, ident)
                if variables.get("FORGE_LANE_NONCE") == correlation_id:
                    matches.append(self._handle(repo, row, variables, dispatch))
            if len(matches) != 1:
                return Inconclusive(
                    reason="pipeline nonce missing or ambiguous",
                    evidence=evidence("probe_dispatch"),
                )
            handle = matches[0]
            proof = evidence("probe_dispatch", pipeline_id=handle.native_id)
            return Found[DispatchOutcome](
                value=Applied[LaneHandle](value=handle, evidence=proof), evidence=proof
            )
        except ReadUnavailable as error:
            return Inconclusive(reason="pipeline read unavailable", evidence=error.evidence)

    async def find_lane(self, repo: RepoRef, correlation_id: str) -> LaneHandle | None:
        result = await self.probe_dispatch(repo, correlation_id)
        if isinstance(result, Found) and isinstance(result.value, Applied):
            return result.value.value
        raise ReadUnavailable("pipeline visibility unknown", result.evidence)

    async def lane_status(self, handle: LaneHandle) -> LaneStatus:
        dispatch = await self.binding(handle.repo, handle.correlation_id)
        row = await self.pipeline(handle.repo, handle.native_id)
        variables = await self.variables(handle.repo, handle.native_id)
        if self._handle(handle.repo, row, variables, dispatch) != handle:
            raise unavailable("lane_execution_changed")
        mapping: dict[
            str, Literal["queued", "running", "succeeded", "failed", "cancelled", "unknown"]
        ] = {
            "passed": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "skipped": "failed",
            "unknown": "unknown",
        }
        state = check_status(row.get("status"))
        return LaneStatus(
            handle=handle,
            status=mapping.get(state, "running" if row.get("status") == "running" else "queued"),
            observed_at=self.clock.now(),
            evidence=evidence("lane_status", pipeline_id=handle.native_id),
        )

    async def checks_for(self, repo: RepoRef, sha: str) -> tuple[CheckResult, ...]:
        candidate_sha = object_sha(sha)
        metadata = obj((await self.api.metadata(repo)).document)
        if (metadata.get("ci_config_path") or ".gitlab-ci.yml") != self.configuration_path:
            raise unavailable("untrusted_ci_configuration_path")
        rows = await self.api.pages(repo, "pipelines", params={"sha": candidate_sha})
        seen = set()
        checks = []
        for raw in rows:
            ident = native_id(raw.get("id"))
            if ident in seen:
                raise unavailable("duplicate_pipeline_page")
            seen.add(ident)
            row = await self.pipeline(repo, ident)
            if row.get("sha") != candidate_sha:
                raise unavailable("pipeline_candidate_mismatch")
            variables = await self.variables(repo, ident)
            lane = "FORGE_LANE_DISPATCH" in variables or "FORGE_LANE_NONCE" in variables
            if lane:
                dispatch = await self.binding(repo, variables.get("FORGE_LANE_NONCE", ""))
                self._handle(repo, row, variables, dispatch)
            elif row.get("source") not in self.verification_sources:
                continue
            checks.extend(
                await self._checks(repo, row, candidate_sha, "lane" if lane else "verification")
            )
        return tuple(checks)

    async def _checks(
        self,
        repo: RepoRef,
        pipeline: dict[str, object],
        candidate_sha: str,
        role: Literal["lane", "verification"],
    ) -> list[CheckResult]:
        ident = native_id(pipeline.get("id"))
        # Native endpoint excludes retried jobs: only the current execution for
        # each job is eligible. A retry's new job ID remains its attempt identity.
        jobs = await self.api.pages(
            repo, "pipelines/" + ident + "/jobs", params={"include_retried": "false"}
        )
        names: set[str] = set()
        ids: set[str] = set()
        checks = []
        for row in jobs:
            job_id, name = native_id(row.get("id")), text(row.get("name"))
            if (
                job_id in ids
                or name in names
                or native_id(obj(row.get("pipeline")).get("id")) != ident
                or obj(row.get("pipeline")).get("sha") != candidate_sha
                or obj(row.get("commit")).get("id") != candidate_sha
            ):
                raise unavailable("job_execution_binding_mismatch")
            ids.add(job_id)
            names.add(name)
            checks.append(
                CheckResult(
                    name=name,
                    tested_sha=candidate_sha,
                    native_id=job_id,
                    native_run_id=ident,
                    attempt=int(job_id),
                    role=role,
                    status=check_status(row.get("status")),
                    observed_at=self.clock.now(),
                    workflow_path=self.configuration_path,
                    failure_kind=failure_kind(row),
                    evidence=evidence("check_job", pipeline_id=ident, job_id=job_id),
                )
            )
        return checks
