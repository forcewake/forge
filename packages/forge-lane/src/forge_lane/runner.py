"""An authenticated attempt generation, from approved input to a reported result."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from forge_protocol import CandidateBundle, LaneDispatch, LaneReport

from .approved import prepare_policy
from .checkpoint import Checkpoints
from .client import LaneClient
from .driver import Driver, DriverEnv
from .drivers.claude_policy import FilePolicy
from .errors import ExitCode, LaneFailure
from .supervisor import Outcome, Supervisor, Timings
from .trace import Platform, Trace
from .workspace import Limits, Workspace


@dataclass(frozen=True)
class RunResult:
    exit_code: ExitCode
    classification: str
    report: LaneReport | None
    report_accepted: bool


class Runner:
    def __init__(
        self,
        dispatch: LaneDispatch,
        client: LaneClient,
        driver: Driver,
        workspace: Path,
        *,
        timings: Timings = Timings(),
        limits: Limits = Limits(),
        trace_platform: Platform | None = None,
    ):
        self.dispatch, self.client, self.driver = dispatch, client, driver
        self.workspace = Workspace(workspace, dispatch.execution_base_sha, limits)
        self.timings = timings
        self.supervisor: Supervisor | None = None
        self.started = monotonic()
        self.trace_platform = trace_platform
        self.trace: Trace | None = None

    async def execute(self, oidc_token: str) -> RunResult:
        self.started = monotonic()
        try:
            await self.client.exchange(oidc_token)
            outcome = await self._execute_approved()
        except LaneFailure as exc:
            outcome = Outcome(
                exc.code,
                "timed_out" if exc.code == ExitCode.DEADLINE else "failed",
                exc.classification,
                exc.detail,
            )
        except asyncio.CancelledError:
            outcome = await self._stop_after_error(
                Outcome(
                    ExitCode.CANCELLED, "cancelled", "process_cancelled", "Lane process cancelled"
                )
            )
        except Exception:
            outcome = await self._stop_after_error(
                Outcome(ExitCode.DRIVER, "failed", "driver_error", "Driver execution failed")
            )
        return await self._finish(outcome)

    async def _stop_after_error(self, outcome: Outcome) -> Outcome:
        if self.supervisor is not None:
            try:
                await self.supervisor.stop()
            except LaneFailure as exc:
                return Outcome(exc.code, "failed", exc.classification, exc.detail)
        return outcome

    async def _execute_approved(self) -> Outcome:
        brief = await self.client.brief()
        await prepare_policy(
            brief,
            self.dispatch,
            self.driver,
            self.workspace.path,
            timeout=max(0, self.dispatch.timeout_seconds - (monotonic() - self.started)),
        )
        spec = brief.frozen_spec
        policy = FilePolicy(self.workspace.path, None, scope=spec.write_policy)
        self.workspace.write_allowed = lambda path: policy.permitted("Write", {"file_path": path})
        await self.workspace.validate()
        if self.driver.name != self.dispatch.driver or "interrupt" not in self.driver.capabilities:
            raise LaneFailure(
                ExitCode.DRIVER,
                "unsupported_driver",
                "Driver must match dispatch and support interruption",
            )
        checkpoints = Checkpoints(self.dispatch, self.client, self.workspace)
        note, resume = "", None
        if self.dispatch.checkpoint_id is not None:
            note, resume = await checkpoints.restore()
        remaining = self.dispatch.timeout_seconds - (monotonic() - self.started)
        if remaining <= 0:
            return Outcome(
                ExitCode.DEADLINE,
                "timed_out",
                "wall_clock_exceeded",
                "Lane wall-clock budget exceeded before driver start",
            )
        grant = await self.client.model_key() if self.driver.requires_model_key else None
        remaining = self.dispatch.timeout_seconds - (monotonic() - self.started)
        if remaining <= 0:
            return Outcome(
                ExitCode.DEADLINE,
                "timed_out",
                "wall_clock_exceeded",
                "Lane wall-clock budget exceeded before driver start",
            )
        env = DriverEnv(
            self.dispatch.model,
            grant,
            resume if "resume_session" in self.driver.capabilities else None,
            note,
        )
        if self.trace_platform is not None:
            self.trace = Trace(
                self.driver.name,
                self.dispatch.model,
                platform=self.trace_platform,
                secrets=(
                    grant.key if grant else "",
                    self.client.token.access_token if self.client.token else "",
                ),
            )
        try:
            session = await asyncio.wait_for(
                self.driver.start(brief, self.workspace.path, env), remaining
            )
        except asyncio.TimeoutError:
            return Outcome(
                ExitCode.DEADLINE,
                "timed_out",
                "wall_clock_exceeded",
                "Driver start exceeded wall-clock budget",
            )
        self.supervisor = Supervisor(
            self.client, self.driver, session, checkpoints, self.timings, self.trace
        )
        return await self.supervisor.run(
            self.dispatch.timeout_seconds - (monotonic() - self.started)
        )

    async def _finish(self, outcome: Outcome) -> RunResult:
        diff_id = None
        try:
            if outcome.code == ExitCode.COMPLETED:
                diff = await self.workspace.diff()
                if not diff:
                    outcome = Outcome(
                        ExitCode.EMPTY_DIFF,
                        "failed",
                        "empty_diff",
                        "Driver produced no candidate changes",
                        usage=outcome.usage,
                    )
                else:
                    diff_id = await self.client.put_blob(diff)
                    await self.client.candidate(
                        CandidateBundle(
                            **self.client.identity,
                            diff_sha256=diff_id,
                            diff_size_bytes=len(diff),
                            base_sha=self.dispatch.execution_base_sha,
                            approved_digest=self.dispatch.approved_digest,
                            driver=self.dispatch.driver,
                            model=self.dispatch.model,
                            terminal_reason=outcome.reason,
                            usage=outcome.usage,
                        )
                    )
        except LaneFailure as exc:
            outcome = Outcome(
                exc.code, "failed", exc.classification, exc.detail, usage=outcome.usage
            )
            diff_id = None
        if self.trace is not None:
            self.trace.finish(outcome.classification, outcome.usage, detail=outcome.reason)
        report = LaneReport(
            **self.client.identity,
            approved_digest=self.dispatch.approved_digest,
            status=outcome.status,
            classification=outcome.classification,
            driver=self.dispatch.driver,
            model=self.dispatch.model,
            terminal_reason=outcome.reason,
            candidate_diff_sha256=diff_id,
            checkpoint_id=outcome.checkpoint_id,
            usage=outcome.usage,
        )
        if self.client.token is None:
            return RunResult(outcome.code, outcome.classification, report, False)
        try:
            await self.client.report(report)
        except LaneFailure as exc:
            return RunResult(exc.code, exc.classification, report, False)
        return RunResult(outcome.code, outcome.classification, report, True)
