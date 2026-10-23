"""Experimental driver contracts against fake JSONL and loopback HTTP protocols."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from forge_lane.cli import load_driver
from forge_lane.contrib.codex import CodexDriver
from forge_lane.contrib.common import SafeFiles, environment, grant_key
from forge_lane.contrib.grok import GrokDriver
from forge_lane.contrib.opencode import ROUTES, RULES, OpenCodeDriver
from forge_lane.driver import DriverEnv
from forge_lane.errors import ExitCode, LaneFailure
from forge_protocol import ModelKeyGrant
from tests.test_lane_runner_v1 import checkout, inputs


def setup(tmp_path):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    grant = ModelKeyGrant(
        forge_run_id=brief.forge_run_id,
        attempt=brief.attempt,
        generation=brief.generation,
        gateway_url="https://gateway.invalid/attempt",
        key="synthetic-attempt-key",
        model="test-model",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return workspace, brief, DriverEnv("test-model", grant)


class Wire:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.received = []

    async def spawn(self, argv, cwd, env):
        self.received.append((argv, cwd, env))
        return self

    async def receive(self):
        return await self.frames.get()

    async def send(self, frame):
        self.sent.append(frame)

    async def close(self):
        await asyncio.sleep(0)
        self.closed = True


class CodexWire(Wire):
    def __init__(self, gateway):
        super().__init__()
        self.gateway = str(gateway)
        self.unsafe = False

    async def send(self, frame):
        await super().send(frame)
        method = frame.get("method")
        result = {}
        if method == "config/read":
            result = {
                "config": {
                    "features": {"shell_tool": self.unsafe},
                    "web_search": "disabled",
                    "apps": {"_default": {"enabled": False}},
                    "model_provider": "forge",
                    "mcp_servers": {},
                    "model_providers": {
                        "forge": {"base_url": self.gateway, "env_key": "FORGE_MODEL_KEY"}
                    },
                }
            }
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1"}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1"}}
        if "id" in frame and method and method != "turn/interrupt":
            self.frames.put_nowait({"id": frame["id"], "result": result})

    def complete(self, status="completed"):
        self.frames.put_nowait(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": status},
                },
            }
        )


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_private_environment_and_grant_identity(tmp_path, monkeypatch):
    workspace, brief, env = setup(tmp_path)
    for variable in ("GITHUB_TOKEN", "CI_JOB_TOKEN", "OPENAI_API_KEY", "CODEX_HOME", "XAI_API_KEY"):
        monkeypatch.setenv(variable, "ambient-must-not-propagate")
    child = environment(tmp_path / "private")
    assert not any(value == "ambient-must-not-propagate" for value in child.values())
    assert child["HOME"] != str(workspace) and child["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert grant_key(brief, env) == "synthetic-attempt-key"
    for mutation in (
        {"generation": brief.generation + 1},
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        {"model": "other"},
    ):
        bad = DriverEnv("test-model", env.model_key.model_copy(update=mutation))
        with pytest.raises(LaneFailure, match="gateway grant"):
            grant_key(brief, bad)


def test_file_tools_refuse_shell_authority_paths_and_symlinks(tmp_path):
    workspace, _, _ = setup(tmp_path)
    policy = SafeFiles(workspace, None)
    target = workspace / "safe.py"
    target.write_text("old")
    target.chmod(0o755)
    assert policy.invoke("read_file", {"file_path": "safe.py"}) == "old"
    policy.invoke("edit_file", {"file_path": "safe.py", "old_string": "old", "new_string": "new"})
    assert target.read_text() == "new" and target.stat().st_mode & 0o777 == 0o755
    policy.invoke("write_file", {"file_path": "new.txt", "content": "new"})
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (workspace / "escape").symlink_to(outside)
    (workspace / ".opencode").mkdir()
    (workspace / ".opencode" / "config.json").write_text("private")
    (workspace / "alias").symlink_to(workspace / ".opencode")
    for path in (
        ".git/config",
        ".env",
        ".github/workflows/test.yml",
        ".codex/config.toml",
        "escape",
        "alias/config.json",
        str(outside),
    ):
        with pytest.raises(LaneFailure):
            policy.invoke("read_file", {"file_path": path})
    for tool, arguments in (
        ("Bash", {"command": "true"}),
        ("read_file", {"file_path": "safe.py", "extra": True}),
        ("write_file", {"file_path": "safe.py", "content": 123}),
    ):
        with pytest.raises(LaneFailure):
            policy.invoke(tool, arguments)
    assert outside.read_text() == "secret"


async def test_codex_handshake_file_tools_completion_and_steer(tmp_path, monkeypatch):
    workspace, brief, env = setup(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "never-forward")
    wire = CodexWire(env.model_key.gateway_url)
    session = await CodexDriver(transport_factory=wire.spawn).start(brief, workspace, env)
    events = asyncio.create_task(collect(session))
    await asyncio.sleep(0)
    assert not events.done()  # turn/start acknowledges delivery, not completion.
    await session.steer("bounded correction")
    steer = next(frame for frame in wire.sent if frame.get("method") == "turn/steer")
    assert steer["params"] == {
        "threadId": "thread-1",
        "expectedTurnId": "turn-1",
        "input": [{"type": "text", "text": "bounded correction"}],
    }
    wire.frames.put_nowait(
        {
            "id": 900,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tool": "write_file",
                "arguments": {"file_path": "result.txt", "content": "approved implementation"},
            },
        }
    )
    await until(lambda: any(frame.get("id") == 900 for frame in wire.sent))
    assert (workspace / "result.txt").read_text() == "approved implementation"
    assert wire.sent[-1]["result"]["success"] is True
    wire.complete()
    result = await asyncio.wait_for(events, 2)
    assert result[-1].kind == "completed" and result[-1].usage is None and wire.closed
    argv, _, child = wire.received[0]
    assert child["FORGE_MODEL_KEY"] == env.model_key.key and "GITHUB_TOKEN" not in child
    assert "features.shell_tool=false" in argv and 'web_search="disabled"' in argv
    thread = next(frame for frame in wire.sent if frame.get("method") == "thread/start")
    assert thread["params"]["sandbox"] == "read-only"
    assert {tool["name"] for tool in thread["params"]["dynamicTools"]} == {
        "read_file",
        "write_file",
        "edit_file",
    }


async def collect(session):
    return [event async for event in session.events()]


async def test_codex_interrupt_is_quiescent(tmp_path):
    workspace, brief, env = setup(tmp_path)
    wire = CodexWire(env.model_key.gateway_url)
    session = await CodexDriver(transport_factory=wire.spawn).start(brief, workspace, env)
    await session.interrupt()  # No RPC interrupt response is needed for local cleanup.
    assert wire.closed and session.reader.done() and session.owner.done()
    assert not [event for event in await collect(session) if event.kind == "completed"]
    assert not Path(wire.received[0][2]["HOME"]).exists()


async def test_codex_refuses_effective_shell_and_foreign_turn_tool(tmp_path):
    workspace, brief, env = setup(tmp_path)
    wire = CodexWire(env.model_key.gateway_url)
    wire.unsafe = True
    with pytest.raises(LaneFailure, match="configuration"):
        await CodexDriver(transport_factory=wire.spawn).start(brief, workspace, env)
    assert wire.closed
    wire = CodexWire(env.model_key.gateway_url)
    session = await CodexDriver(transport_factory=wire.spawn).start(brief, workspace, env)
    wire.frames.put_nowait(
        {
            "id": 900,
            "method": "item/tool/call",
            "params": {
                "threadId": "thread-1",
                "turnId": "foreign",
                "tool": "write_file",
                "arguments": {"file_path": "must-not-exist", "content": "wrong turn"},
            },
        }
    )
    with pytest.raises(LaneFailure):
        await collect(session)
    assert not (workspace / "must-not-exist").exists() and wire.closed


class OpenCodeHTTP:
    def __init__(self, workspace):
        self.workspace = workspace
        self.requests = []
        self.started = False
        self.terminal = False
        self.permission = None
        self.permission_echo = RULES
        self.polled = asyncio.Event()
        self.cold = True

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        if path == "/openapi.json":
            if self.cold:
                self.cold = False
                raise httpx.ConnectError("not listening yet", request=request)
            # Placeholder spelling is not a protocol difference.
            return httpx.Response(
                200, json={"paths": {path.replace("sessionID", "id"): {} for path in ROUTES}}
            )
        if path == "/api/session":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "session-1",
                        "directory": str(self.workspace),
                        "permissions": self.permission_echo,
                    }
                },
            )
        if path.endswith("/prompt"):
            self.started = True
        if path.endswith("/message"):
            self.polled.set()
            messages = (
                []
                if not self.started
                else [
                    {
                        "id": "assistant-1",
                        "type": "assistant",
                        "finish": "stop" if self.terminal else "tool-calls",
                        "content": [{"type": "text", "text": "done"}],
                    }
                ]
            )
            return httpx.Response(200, json={"data": messages})
        if path.endswith("/reply"):
            self.permission = None
        if path.endswith("/permission"):
            return httpx.Response(200, json={"data": [self.permission] if self.permission else []})
        return httpx.Response(204)


async def test_opencode_sets_model_permissions_and_waits_for_terminal_transcript(tmp_path):
    workspace, brief, env = setup(tmp_path)
    wire, http = Wire(), OpenCodeHTTP(workspace)
    driver = OpenCodeDriver(server_factory=wire.spawn, transport=httpx.MockTransport(http))
    session = await driver.start(brief, workspace, env)
    events = asyncio.create_task(collect(session))
    await http.polled.wait()
    await asyncio.sleep(0)
    assert not events.done()
    model = next(body for _, path, body in http.requests if path.endswith("/model"))
    assert model == {"model": {"providerID": "forge", "id": "test-model"}}
    assert driver.capabilities == frozenset({"interrupt"})
    http.terminal = True
    result = await asyncio.wait_for(events, 2)
    assert result[-1].kind == "completed" and wire.closed and result[-1].usage is None
    argv, _, child = wire.received[0]
    assert argv[argv.index("--hostname") + 1] == "127.0.0.1"
    assert child["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1" and child["OPENCODE_PURE"] == "1"
    config = json.loads(child["OPENCODE_CONFIG_CONTENT"])
    assert config["enabled_providers"] == ["forge"] and config["mcp"] == {}
    assert config["provider"]["forge"]["options"]["apiKey"] == "{env:FORGE_MODEL_KEY}"


async def test_opencode_rejects_shell_permission_and_unconfirmed_rules(tmp_path):
    workspace, brief, env = setup(tmp_path)
    wire, http = Wire(), OpenCodeHTTP(workspace)
    http.permission_echo = []
    with pytest.raises(LaneFailure, match="permission isolation"):
        await OpenCodeDriver(server_factory=wire.spawn, transport=httpx.MockTransport(http)).start(
            brief, workspace, env
        )
    assert wire.closed and not http.started
    wire, http = Wire(), OpenCodeHTTP(workspace)
    http.permission = {
        "id": "per-1",
        "sessionID": "session-1",
        "action": "bash",
        "resources": ["git push"],
    }
    session = await OpenCodeDriver(
        server_factory=wire.spawn, transport=httpx.MockTransport(http)
    ).start(brief, workspace, env)
    with pytest.raises(LaneFailure):
        await collect(session)
    assert (
        "POST",
        "/api/session/session-1/permission/per-1/reply",
        {"decision": "reject"},
    ) in http.requests
    assert wire.closed


async def test_grok_has_restricted_tools_explicit_auth_and_unknown_usage(tmp_path, monkeypatch):
    workspace, brief, _ = setup(tmp_path)
    env = DriverEnv("subscription-model")
    monkeypatch.delenv("FORGE_GROK_AUTH", raising=False)
    monkeypatch.setenv("XAI_API_KEY", "ambient-key")
    wire = Wire()
    with pytest.raises(LaneFailure, match="explicit masked"):
        await GrokDriver(transport_factory=wire.spawn).start(brief, workspace, env)
    monkeypatch.setenv(
        "FORGE_GROK_AUTH", json.dumps({"access_token": "synthetic-subscription-secret"})
    )
    session = await GrokDriver(transport_factory=wire.spawn).start(brief, workspace, env)
    argv, _, child = wire.received[0]
    assert not {"--always-approve", "--trust", "--yolo"} & set(argv)
    assert argv[argv.index("--tools") + 1] == "Read,Write,Edit"
    assert (
        all(value != "ambient-key" for value in child.values()) and "FORGE_GROK_AUTH" not in child
    )
    config = Path(child["GROK_HOME"])
    assert (config / "auth.json").stat().st_mode & 0o777 == 0o600
    assert 'extends = "strict"' in (config / "sandbox.toml").read_text()
    wire.frames.put_nowait({"type": "text", "text": "visible synthetic-subscription-secret"})
    wire.frames.put_nowait({"params": {"update": {"type": "end", "stopReason": "end_turn"}}})
    wire.frames.put_nowait(None)
    events = await collect(session)
    assert events[-1].kind == "completed" and events[-1].usage is None and wire.closed
    assert "synthetic-subscription-secret" not in events[0].text and not config.exists()
    assert GrokDriver.capabilities == frozenset({"interrupt"})


async def test_grok_eof_does_not_invent_success(tmp_path, monkeypatch):
    workspace, brief, _ = setup(tmp_path)
    monkeypatch.setenv("FORGE_GROK_AUTH", '{"access_token":"synthetic"}')
    wire = Wire()
    session = await GrokDriver(transport_factory=wire.spawn).start(
        brief, workspace, DriverEnv("model")
    )
    wire.frames.put_nowait(None)
    with pytest.raises(LaneFailure, match="confirm"):
        await collect(session)
    assert wire.closed


def test_contrib_requires_explicit_runtime_activation(monkeypatch):
    monkeypatch.delenv("FORGE_LANE_ENABLE_CONTRIB", raising=False)
    for name in ("codex", "opencode", "grok"):
        with pytest.raises(LaneFailure) as error:
            load_driver(name)
        assert error.value.classification == "contrib_disabled"
    monkeypatch.setenv("FORGE_LANE_ENABLE_CONTRIB", "1")
    assert [load_driver(name).name for name in ("codex", "opencode", "grok")] == [
        "codex",
        "opencode",
        "grok",
    ]


async def test_opencode_grants_only_one_confined_file_operation(tmp_path):
    workspace, brief, env = setup(tmp_path)
    wire, http = Wire(), OpenCodeHTTP(workspace)
    http.permission = {
        "id": "per-1",
        "sessionID": "session-1",
        "action": "write",
        "resources": ["result.py"],
    }
    session = await OpenCodeDriver(
        server_factory=wire.spawn, transport=httpx.MockTransport(http)
    ).start(brief, workspace, env)
    try:
        await until(lambda: any(path.endswith("/reply") for _, path, _ in http.requests))
        assert (
            "POST",
            "/api/session/session-1/permission/per-1/reply",
            {"decision": "once"},
        ) in http.requests
    finally:
        await session.interrupt()
    assert wire.closed and session.owner.done()


@pytest.mark.parametrize("name", ["opencode", "grok"])
async def test_interrupt_does_not_swallow_process_cleanup_failure(tmp_path, monkeypatch, name):
    workspace, brief, env = setup(tmp_path)
    wire = Wire()

    async def cannot_stop():
        raise LaneFailure(ExitCode.DRIVER, "cleanup_failed", "synthetic process did not stop")

    wire.close = cannot_stop
    if name == "opencode":
        http = OpenCodeHTTP(workspace)
        session = await OpenCodeDriver(
            server_factory=wire.spawn, transport=httpx.MockTransport(http)
        ).start(brief, workspace, env)
        await until(lambda: any(path.endswith("/permission") for _, path, _ in http.requests))
    else:
        monkeypatch.setenv("FORGE_GROK_AUTH", '{"access_token":"synthetic"}')
        session = await GrokDriver(transport_factory=wire.spawn).start(
            brief, workspace, DriverEnv("model")
        )
        await asyncio.sleep(0)
    with pytest.raises(LaneFailure, match="did not stop"):
        await session.interrupt()
    assert session.cleanup_task.done() and session.owner.done()
