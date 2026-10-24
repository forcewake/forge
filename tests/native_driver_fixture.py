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
from forge_protocol import CheckpointManifest
from tests import test_lane_claude_code as claude
from tests import test_lane_copilot_cli as copilot
from tests import test_lane_runner_v1 as lane

FakeSDK, Result, FakeACP = claude.FakeSDK, claude.Result, copilot.FakeACP


class Case:
    def __init__(self, root, *, driver="claude_code", clone_from=None, **changes):
        root.mkdir(parents=True, exist_ok=True)
        self.root, self.workspace = root, root / "work"
        if clone_from is None:
            self.base = lane.checkout(self.workspace)
        else:
            lane.git(root, "clone", "-q", str(clone_from), str(self.workspace))
            self.base = lane.git(self.workspace, "rev-parse", "HEAD").decode().strip()
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


class ScriptClient(claude.FakeClient):
    async def query(self, text):
        self.prompts.append(text)
        if len(self.prompts) == 1:
            if self.sdk.edit:
                self.sdk.edit(Path(self.options.cwd))
            for message in self.sdk.frames:
                self.messages.put_nowait(message)

    async def receive_messages(self):
        while (message := await self.messages.get()) is not None:
            yield message


async def completed_case(root, *, frames=None, edit=True, mutate=None, **dispatch):
    case = Case(root, **dispatch)
    case.sdk.frames = (
        frames
        if frames is not None
        else [Result(usage={"input_tokens": 10, "output_tokens": 20}, total_cost_usd=0.1)]
    )
    case.sdk.edit = (
        (lambda workspace: (workspace / "base.txt").write_text("approved change\n"))
        if edit
        else None
    )
    case.sdk.ClaudeSDKClient = lambda options: ScriptClient(case.sdk, options)
    if mutate:
        mutate(case)
    case.driver = ClaudeCodeDriver(case.sdk)
    case.result = await case.execute(case.driver)
    return case


async def restored_case(root, *, corrupt=False, requested=True):
    first = await completed_case(
        root / "first", frames=[], mutate=lambda case: case.server.command("pause")
    )
    assert first.result.report.status == "paused" and first.server.checkpoints
    manifest = first.server.checkpoints[-1]
    seen = []

    def restore(case):
        case.server.checkpoints = first.server.checkpoints
        case.server.blobs = first.server.blobs
        case.server.corrupt_blob = corrupt
        case.sdk.edit = lambda workspace: seen.append((workspace / "base.txt").read_text())

    second = await completed_case(
        root / "second",
        clone_from=first.workspace,
        checkpoint_id=CheckpointManifest.model_validate(manifest).checkpoint_id
        if requested
        else None,
        generation=1,
        mutate=restore,
    )
    if not corrupt:
        assert seen == ["approved change\n" if requested else "base\n"]
    return SimpleNamespace(first=first, second=second, manifest=manifest, seen=seen)


async def copilot_case(root, monkeypatch, *, reason="end_turn", controls=(), **dispatch):
    case = Case(root, driver="copilot_cli", **dispatch)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "synthetic-copilot-only")

    class ACP(FakeACP):
        async def send(self, frame):
            await super().send(frame)
            if frame.get("method") == "session/prompt":
                (case.workspace / "base.txt").write_text("Copilot candidate\n")
                if reason == "eof":
                    self.frames.put_nowait(None)
                elif reason == "rpc_error":
                    self.frames.put_nowait(
                        {
                            "jsonrpc": "2.0",
                            "id": frame["id"],
                            "error": {"message": "vendor detail must stay private"},
                        }
                    )
                elif reason is not None:
                    self.respond(frame["id"], {"stopReason": reason})

    wire = ACP(automatic=False)
    case.wire = wire

    async def transport(binary, cwd, config, token, model, emit):
        case.received.append(
            SimpleNamespace(binary=binary, cwd=cwd, config=config, token=token, model=model)
        )
        return wire

    for action, text in controls:
        case.server.command(action, len(case.server.commands) + 1, text)
    case.driver = CopilotCLIDriver(transport_factory=transport)
    case.result = await case.execute(case.driver)
    return case
