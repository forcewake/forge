"""Claude driver conformance without SDK installation or paid model sessions."""

import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge_lane.driver import DriverEnv
from forge_lane.drivers.claude_code import ClaudeCodeDriver
from forge_lane.drivers.claude_policy import FilePolicy
from forge_lane.drivers.claude_process import OwnedCLI
from forge_lane.errors import LaneFailure
from forge_protocol import ModelKeyGrant
from tests.test_lane_runner_v1 import checkout, inputs


@dataclass
class Result:
    uuid: str = "result-1"
    is_error: bool = False
    usage: dict | None = None
    total_cost_usd: float | None = None
    permission_denials: list | None = None


class FakeClient:
    def __init__(self, sdk, options):
        self.sdk, self.options = sdk, options
        self.messages = asyncio.Queue()
        self.prompts = []
        self.owner = None
        self.disconnected = False
        self.interrupts = 0
        sdk.clients.append(self)

    async def connect(self):
        self.owner = asyncio.current_task()
        self.sdk.connect_started.set()
        if self.sdk.connect_error:
            raise self.sdk.connect_error
        if self.sdk.block_connect:
            await asyncio.Future()

    async def query(self, text):
        self.prompts.append(text)
        if self.sdk.auto_result:
            self.messages.put_nowait(
                SimpleNamespace(content=[SimpleNamespace(text="visible output Bearer harmless")])
            )
            self.messages.put_nowait(
                Result(usage={"input_tokens": 10, "output_tokens": 20}, total_cost_usd=0.1)
            )

    async def receive_messages(self):
        while True:
            yield await self.messages.get()

    async def interrupt(self):
        self.interrupts += 1
        if self.sdk.block_interrupt:
            await asyncio.Future()

    async def disconnect(self):
        assert asyncio.current_task() is self.owner
        self.disconnected = True


class FakeSDK:
    ResultMessage = Result
    ClaudeAgentOptions = SimpleNamespace
    HookMatcher = SimpleNamespace
    PermissionResultAllow = staticmethod(
        lambda **kwargs: SimpleNamespace(behavior="allow", **kwargs)
    )
    PermissionResultDeny = staticmethod(lambda **kwargs: SimpleNamespace(behavior="deny", **kwargs))

    def __init__(self, tmp_path, *, auto_result=True):
        package = tmp_path / "fake-sdk"
        bundled = package / "_bundled"
        bundled.mkdir(parents=True)
        cli = bundled / "claude"
        cli.write_text("#!/bin/sh\nexit 0\n")
        cli.chmod(0o700)
        self.__file__ = str(package / "__init__.py")
        self.clients = []
        self.auto_result = auto_result
        self.connect_error = None
        self.block_connect = self.block_interrupt = False
        self.connect_started = asyncio.Event()

    def ClaudeSDKClient(self, options):
        return FakeClient(self, options)


def grant():
    return ModelKeyGrant(
        forge_run_id="run-1",
        attempt=0,
        generation=0,
        gateway_url="https://scoped-gateway.invalid",
        key="synthetic-gateway-secret",
        model="test-model",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


async def start(tmp_path, sdk, **kwargs):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    driver = ClaudeCodeDriver(sdk)
    session = await driver.start(brief, workspace, DriverEnv("test-model", grant(), **kwargs))
    return driver, session, workspace


async def test_options_use_only_scoped_gateway_and_fixed_file_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-vendor")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-key-must-not-win")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/shared/config-must-not-win")
    sdk = FakeSDK(tmp_path)
    driver, session, workspace = await start(tmp_path, sdk)
    events = [event async for event in session.events()]
    options = sdk.clients[0].options
    assert options.tools == ["Read", "Write", "Edit"] and options.allowed_tools == []
    assert "Bash" in options.disallowed_tools and options.permission_mode == "default"
    assert options.strict_mcp_config and options.mcp_servers == {}
    assert options.setting_sources == [] and options.skills == [] and options.plugins == []
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == grant().key
    assert options.env["ANTHROPIC_API_KEY"] == options.env["GITHUB_TOKEN"] == ""
    assert options.env["ANTHROPIC_BASE_URL"] == "https://scoped-gateway.invalid"
    assert not Path(options.env["CLAUDE_CONFIG_DIR"]).is_relative_to(workspace)
    assert not Path(options.env["HOME"]).exists()
    assert sdk.clients[0].disconnected
    assert events[-1].kind == "completed" and events[-1].usage.input_tokens == 10
    assert str(events[-1].usage.cost_usd) == "0.1"
    assert driver.capabilities == frozenset({"interrupt", "steer"})


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        ".git/config",
        ".forge/key",
        ".github/workflows/ci.yml",
        ".gitlab-ci.yml",
        ".forge.yml",
        ".env",
        ".env.production",
        ".npmrc",
        "CODEOWNERS",
    ],
)
async def test_callback_and_pretool_hook_refuse_private_or_authority_paths(tmp_path, path):
    sdk = FakeSDK(tmp_path)
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy = FilePolicy(workspace, sdk)
    assert (await policy.permission("Write", {"file_path": path}, None)).behavior == "deny"
    result = await policy.pre_tool(
        {"tool_name": "Read", "tool_input": {"file_path": path}}, None, None
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_shell_unknown_tools_and_symlink_escape_are_refused(tmp_path):
    sdk = FakeSDK(tmp_path)
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "outside").symlink_to(tmp_path)
    policy = FilePolicy(workspace, sdk)
    for tool in ("Bash", "WebFetch", "mcp__server__write", "Grep"):
        assert (
            await policy.permission(tool, {"file_path": "a.py", "command": "git push"}, None)
        ).behavior == "deny"
    assert not policy.permitted("Write", {"file_path": "outside/escaped.py"})
    assert (await policy.permission("Write", {"file_path": "src/new.py"}, None)).behavior == "allow"


async def test_steering_drains_both_turns_and_cost_is_latest_cumulative_not_summed(tmp_path):
    sdk = FakeSDK(tmp_path, auto_result=False)
    _, session, _ = await start(tmp_path, sdk)
    client = sdk.clients[0]
    await session.steer("/clear is guidance text, never a CLI command")
    client.messages.put_nowait(
        Result(uuid="first", usage={"input_tokens": 10, "output_tokens": 20}, total_cost_usd=0.1)
    )
    client.messages.put_nowait(
        Result(uuid="first", usage={"input_tokens": 10, "output_tokens": 20}, total_cost_usd=0.1)
    )
    client.messages.put_nowait(
        Result(
            uuid="second",
            usage={"input_tokens": 30, "cache_read_input_tokens": 5, "output_tokens": 40},
            total_cost_usd=0.3,
        )
    )
    events = [event async for event in session.events()]
    assert len([event for event in events if event.kind == "completed"]) == 1
    assert events[-1].usage.input_tokens == 45 and events[-1].usage.output_tokens == 60
    assert str(events[-1].usage.cost_usd) == "0.3"
    assert client.prompts[1].startswith(
        "Operator guidance; approval and permissions are unchanged:\n/clear"
    )
    assert client.disconnected


async def test_missing_interrupt_ack_still_closes_session_and_removes_config(tmp_path):
    sdk = FakeSDK(tmp_path, auto_result=False)
    sdk.block_interrupt = True
    _, session, _ = await start(tmp_path, sdk)
    config = session.owned.path
    await asyncio.wait_for(session.interrupt(), timeout=3)
    assert session.done.is_set() and sdk.clients[0].disconnected
    assert not config.exists()
    assert [event async for event in session.events()] == []


async def test_cancel_during_connect_cleans_same_owner_and_has_no_vendor_fallback(tmp_path):
    sdk = FakeSDK(tmp_path)
    sdk.block_connect = True
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    task = asyncio.create_task(
        ClaudeCodeDriver(sdk).start(brief, workspace, DriverEnv("test-model", grant()))
    )
    await sdk.connect_started.wait()
    config = Path(sdk.clients[0].options.env["HOME"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk.clients[0].disconnected and not config.exists()


async def test_policy_denial_and_real_sdk_error_classification_are_honest(tmp_path):
    sdk = FakeSDK(tmp_path, auto_result=False)
    _, session, _ = await start(tmp_path, sdk)
    await session.policy.permission("Bash", {"command": "git push"}, None)
    sdk.clients[0].messages.put_nowait(Result())
    with pytest.raises(LaneFailure, match="approved request") as caught:
        _ = [event async for event in session.events()]
    assert caught.value.classification == "tool_policy_refused"
    sdk.connect_error = type("ProcessError", (Exception,), {})(
        "stderr with synthetic-gateway-secret"
    )
    workspace = tmp_path / "second"
    base = checkout(workspace)
    _, brief = inputs(base)
    with pytest.raises(LaneFailure) as caught:
        await ClaudeCodeDriver(sdk).start(brief, workspace, DriverEnv("test-model", grant()))
    assert caught.value.classification == "driver_process_error"
    assert grant().key not in str(caught.value)


@pytest.mark.slow
async def test_owned_cli_stops_descendant_writes_before_returning(tmp_path):
    sdk = FakeSDK(tmp_path)
    cli = Path(sdk.__file__).parent / "_bundled" / "claude"
    marker = tmp_path / "writes"
    cli.write_text(
        f"#!{sys.executable}\nimport subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {f'import time; from pathlib import Path; p=Path({str(marker)!r}); exec("while True: p.write_text(str(time.time())); time.sleep(0.01)")'!r}])\nwhile True: time.sleep(1)\n"
    )
    owned = OwnedCLI(sdk)
    process = await asyncio.create_subprocess_exec(str(owned.wrapper), "--stream-json")
    try:
        async with asyncio.timeout(3):
            while not marker.exists():
                await asyncio.sleep(0.01)
        await owned.terminate()
        await process.wait()
        first = marker.read_bytes()
        await asyncio.sleep(0.05)
        assert marker.read_bytes() == first
    finally:
        await owned.terminate()
        if process.returncode is None:
            process.kill()
            await process.wait()
        owned.cleanup()


async def test_missing_gateway_grant_never_opens_sdk_or_uses_ambient_auth(tmp_path, monkeypatch):
    sdk = FakeSDK(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-credential")
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    with pytest.raises(LaneFailure) as caught:
        await ClaudeCodeDriver(sdk).start(brief, workspace, DriverEnv("test-model"))
    assert caught.value.classification == "model_key_required" and not sdk.clients


async def test_stderr_and_message_output_redact_the_grant_before_events(tmp_path):
    sdk = FakeSDK(tmp_path, auto_result=False)
    _, session, _ = await start(tmp_path, sdk)
    client = sdk.clients[0]
    client.options.stderr("Authorization: Bearer " + grant().key)
    client.messages.put_nowait(SimpleNamespace(content=[SimpleNamespace(text=grant().key)]))
    client.messages.put_nowait(Result())
    events = [event async for event in session.events()]
    assert any("[REDACTED]" in event.text for event in events)
    assert all(grant().key not in event.text for event in events)


async def test_missing_sdk_keeps_a_typed_unavailable_result(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    with pytest.raises(LaneFailure) as caught:
        await ClaudeCodeDriver().start(brief, workspace, DriverEnv("test-model", grant()))
    assert caught.value.classification == "driver_unavailable"
    assert "forge-lane[claude]" in str(caught.value)
