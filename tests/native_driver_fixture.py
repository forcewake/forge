"""Native SDK/ACP boundaries and a real checkout with immutable approved protocol bytes."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx

from forge_lane.client import LaneClient
from forge_lane.driver import DriverEnv
from forge_lane.drivers.claude_code import ClaudeCodeDriver
from forge_lane.drivers.copilot_cli import CopilotCLIDriver
from forge_lane.runner import Runner
from forge_lane.supervisor import Timings
from tests import test_lane_claude_code as claude
from tests import test_lane_copilot_cli as copilot
from tests import test_lane_runner_v1 as lane

FakeSDK, Result, FakeACP = claude.FakeSDK, claude.Result, copilot.FakeACP


class Case:
    def __init__(self, root, *, driver="claude_code", **changes):
        self.root, self.workspace = root, root / "work"
        self.base = lane.checkout(self.workspace)
        self.dispatch, self.brief = lane.inputs(self.base, driver=driver, **changes)
        self.server = lane.WireServer(self.dispatch, self.brief)
        self.sdk = FakeSDK(root, auto_result=False)
        self.created, self.received = [], []

    async def claude(self, **env):
        driver = ClaudeCodeDriver(self.sdk)
        session = await driver.start(
            self.brief,
            self.workspace,
            DriverEnv(
                self.dispatch.model,
                claude.grant().model_copy(
                    update={
                        **self.dispatch.model_dump(
                            include={"forge_run_id", "attempt", "generation"}
                        ),
                        "model": self.dispatch.model,
                    }
                ),
                **env,
            ),
        )
        self.created.append(session)
        return session

    async def copilot(self, wire, monkeypatch):
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "synthetic-copilot-only")

        async def transport(binary, cwd, config, token, model, emit):
            self.received.append(
                SimpleNamespace(binary=binary, cwd=cwd, config=config, token=token, model=model)
            )
            return wire

        driver = CopilotCLIDriver(transport_factory=transport)
        session = await driver.start(self.brief, self.workspace, DriverEnv(self.dispatch.model))
        self.created.append(session)
        return session

    async def execute(self, driver, **options):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app)) as http:
            runner = Runner(
                self.dispatch,
                LaneClient(self.dispatch, http),
                driver,
                self.workspace,
                timings=Timings(controls=0.005, heartbeat=0.005, checkpoint=10, shutdown=0.1),
                **options,
            )
            return await runner.execute("ci-oidc")


async def collect(session):
    return [event async for event in session.events()]


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def config_path(session):
    return session.owned.path if hasattr(session, "owned") else Path(session.config.name)
