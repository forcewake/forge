"""Authorized operator actions never manufacture execution-step authority."""

from collections.abc import Mapping

from forge.command_values import CommandEnvelope, CommandLease, CommandResult
from forge.engine.commands import Action, refusal
from forge.ports import Clock, RepoPort
from forge.store.command_runs import CommandRuns
from forge.store.controls import ControlStore
from forge.store.records import RunRecord
from forge.store.resume import ResumeStore
from forge.store.retry import RetryStore


class OperatorActions:
    def __init__(
        self,
        runs: CommandRuns,
        retry: RetryStore,
        controls: ControlStore,
        resume: ResumeStore,
        repo: RepoPort,
        clock: Clock,
    ) -> None:
        self.runs, self.retry_store, self.controls, self.resume_store = (
            runs,
            retry,
            controls,
            resume,
        )
        self.repo, self.clock = repo, clock

    @property
    def actions(self) -> Mapping[str, Action]:
        return {
            "retry": self.retry,
            "pause": self.control,
            "steer": self.control,
            "resume": self.resume,
        }

    async def retry(
        self, lease: CommandLease, envelope: CommandEnvelope, run: RunRecord | None
    ) -> CommandResult:
        if run is None:
            return refusal("run_not_found")
        head = None
        if run.failed_handler is not None and run.failed_handler.value in {
            "dispatch",
            "await_lane",
        }:
            subject = await self.runs.subject(run.id)
            head = await self.repo.head(subject.repo, f"forge/{run.root_run_id}")
        return await self.retry_store.retry(lease, run.id, head, now=self.clock.now())

    async def control(
        self, lease: CommandLease, envelope: CommandEnvelope, run: RunRecord | None
    ) -> CommandResult:
        return (
            refusal("run_not_found")
            if run is None
            else await self.controls.control(lease, run.id, now=self.clock.now())
        )

    async def resume(
        self, lease: CommandLease, envelope: CommandEnvelope, run: RunRecord | None
    ) -> CommandResult:
        if run is None:
            return refusal("run_not_found")
        subject = await self.runs.subject(run.id)
        head = await self.repo.head(subject.repo, f"forge/{run.root_run_id}")
        return await self.resume_store.resume(lease, run.id, head, now=self.clock.now())
