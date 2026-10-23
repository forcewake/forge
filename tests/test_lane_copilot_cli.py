"""Copilot ACP conformance against a deterministic first-party wire stand-in."""

import asyncio

import pytest

from forge_lane.driver import DriverEnv
from forge_lane.drivers.copilot_cli import CopilotCLIDriver
from forge_lane.drivers.copilot_transport import COPILOT_VERSION, environment
from forge_lane.errors import LaneFailure
from tests.test_lane_runner_v1 import checkout, inputs


class FakeACP:
    def __init__(self, *, automatic=True, permission=None, early=False):
        self.frames = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.automatic = automatic
        self.permission = permission
        self.early = early
        self.prompt_id = None
        self.on_cancel = lambda: None

    async def send(self, frame):
        self.sent.append(frame)
        method = frame.get("method")
        if method == "initialize":
            self.respond(
                frame["id"],
                {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "Copilot", "version": COPILOT_VERSION},
                },
            )
        elif method == "session/new":
            if self.early:
                self.frames.put_nowait(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": "session-1",
                            "update": {
                                "sessionUpdate": "available_commands_update",
                                "availableCommands": [],
                            },
                        },
                    }
                )
            self.respond(frame["id"], {"sessionId": "session-1"})
        elif method == "session/prompt":
            self.prompt_id = frame["id"]
            if self.permission is not None:
                self.frames.put_nowait(
                    {
                        "jsonrpc": "2.0",
                        "id": 1000,
                        "method": "session/request_permission",
                        "params": {"sessionId": "session-1", **self.permission},
                    }
                )
            elif self.automatic:
                self.respond(self.prompt_id, {"stopReason": "end_turn"})
        elif method == "session/cancel":
            self.on_cancel()
            self.respond(self.prompt_id, {"stopReason": "end_turn"})
        elif frame.get("id") == 1000:
            self.respond(self.prompt_id, {"stopReason": "end_turn"})

    def respond(self, identity, result):
        self.frames.put_nowait({"jsonrpc": "2.0", "id": identity, "result": result})

    async def receive(self):
        return await self.frames.get()

    async def close(self):
        self.closed = True


async def start(tmp_path, monkeypatch, wire, *, factory=None):
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-secret-only")
    received = []

    async def transport(binary, cwd, config, token, model, emit):
        received.append(dict(binary=binary, workspace=cwd, config=config, token=token, model=model))
        return wire

    driver = CopilotCLIDriver(transport_factory=factory or transport)
    session = await driver.start(brief, workspace, DriverEnv("test-model"))
    return driver, session, received


async def test_copilot_initializes_one_turn_without_fake_usage_or_false_capabilities(
    tmp_path, monkeypatch
):
    wire = FakeACP(early=True)
    driver, session, received = await start(tmp_path, monkeypatch, wire)
    events = [event async for event in session.events()]
    assert events[-1].kind == "completed" and events[-1].usage is None
    assert "unmetered" in events[-1].terminal_reason
    assert driver.capabilities == frozenset({"interrupt"}) and not driver.requires_model_key
    assert wire.closed and not received[0]["config"].exists()
    init = wire.sent[0]
    assert init["method"] == "initialize" and init["params"]["protocolVersion"] == 1
    assert init["params"]["clientCapabilities"]["terminal"] is False
    assert wire.sent[1]["params"]["mcpServers"] == []
    assert [frame.get("method") for frame in wire.sent].count("session/prompt") == 1
    assert received[0]["token"] == "copilot-secret-only" and received[0]["model"] == "test-model"
    with pytest.raises(LaneFailure) as caught:
        await session.steer("new work")
    assert caught.value.classification == "steer_unsupported"


async def test_cancel_ledger_is_written_before_notification_and_end_turn_is_not_success(
    tmp_path, monkeypatch
):
    wire = FakeACP(automatic=False)
    _, session, _ = await start(tmp_path, monkeypatch, wire)
    async with asyncio.timeout(2):
        while wire.prompt_id is None:
            await asyncio.sleep(0)
    observed = []
    wire.on_cancel = lambda: observed.append(session.cancelled)
    await session.interrupt()
    assert observed == [True] and wire.closed
    assert not [event async for event in session.events() if event.kind == "completed"]
    assert any(frame.get("method") == "session/cancel" for frame in wire.sent)


@pytest.mark.parametrize(
    "kind,path",
    [
        ("execute", "src/a.py"),
        ("edit", ".github/workflows/x.yml"),
        ("edit", "../outside"),
        ("fetch", "src/a.py"),
    ],
)
async def test_unsafe_permission_is_cancelled_and_a_zero_output_end_turn_is_refused(
    tmp_path, monkeypatch, kind, path
):
    wire = FakeACP(
        permission={
            "toolCall": {"kind": kind, "rawInput": {"path": path}, "locations": [{"path": path}]},
            "options": [{"kind": "allow_once", "optionId": "yes"}],
        }
    )
    _, session, _ = await start(tmp_path, monkeypatch, wire)
    with pytest.raises(LaneFailure) as caught:
        _ = [event async for event in session.events()]
    assert caught.value.classification == "tool_policy_refused"
    response = next(frame for frame in wire.sent if frame.get("id") == 1000)
    assert response["result"]["outcome"] == {"outcome": "cancelled"}
    assert wire.closed


async def test_safe_file_permission_selects_once_never_always(tmp_path, monkeypatch):
    wire = FakeACP(
        permission={
            "toolCall": {
                "kind": "edit",
                "rawInput": {"path": "src/a.py"},
                "locations": [{"path": "src/a.py"}],
            },
            "options": [
                {"kind": "allow_always", "optionId": "forever"},
                {"kind": "allow_once", "optionId": "once"},
            ],
        }
    )
    _, session, _ = await start(tmp_path, monkeypatch, wire)
    events = [event async for event in session.events()]
    response = next(frame for frame in wire.sent if frame.get("id") == 1000)
    assert response["result"]["outcome"] == {"outcome": "selected", "optionId": "once"}
    assert events[-1].kind == "completed"


async def test_repository_token_is_never_a_copilot_auth_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "repository-token")
    monkeypatch.setenv("GH_TOKEN", "another-repository-token")
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    called = False

    async def forbidden(*args):
        nonlocal called
        called = True
        raise AssertionError("No vendor process without explicit Copilot token")

    with pytest.raises(LaneFailure) as caught:
        await CopilotCLIDriver(transport_factory=forbidden).start(
            brief, workspace, DriverEnv("test-model")
        )
    assert caught.value.classification == "copilot_token_required" and not called


async def test_eof_midturn_is_a_typed_failure_and_releases_private_state(tmp_path, monkeypatch):
    wire = FakeACP(automatic=False)
    _, session, received = await start(tmp_path, monkeypatch, wire)
    async with asyncio.timeout(2):
        while wire.prompt_id is None:
            await asyncio.sleep(0)
    wire.frames.put_nowait(None)
    with pytest.raises(LaneFailure) as caught:
        _ = [event async for event in session.events()]
    assert caught.value.classification == "driver_process_error"
    assert wire.closed and not received[0]["config"].exists()


def test_child_environment_drops_all_ambient_auth_and_byok_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "repository-token")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "byok-must-not-win")
    monkeypatch.setenv("COPILOT_ALLOW_ALL", "true")
    monkeypatch.setenv("COPILOT_HOME", "/shared/home")
    env = environment(tmp_path, "copilot-only")
    assert (
        "GITHUB_TOKEN" not in env
        and "GH_TOKEN" not in env
        and "COPILOT_PROVIDER_API_KEY" not in env
    )
    assert env["COPILOT_GITHUB_TOKEN"] == "copilot-only"
    assert env["COPILOT_ALLOW_ALL"] == "false" and env["COPILOT_AUTO_UPDATE"] == "false"
    assert env["COPILOT_HOME"] == str(tmp_path / "copilot")


@pytest.mark.parametrize(
    "raw", [None, {"path": ".forge/key"}, {"path": "src/a.py", "command": "git push"}]
)
async def test_informational_location_is_not_authority_for_a_missing_or_different_tool_input(
    tmp_path, monkeypatch, raw
):
    wire = FakeACP(
        permission={
            "toolCall": {"kind": "edit", "rawInput": raw, "locations": [{"path": "src/a.py"}]},
            "options": [{"kind": "allow_once", "optionId": "yes"}],
        }
    )
    _, session, _ = await start(tmp_path, monkeypatch, wire)
    with pytest.raises(LaneFailure) as caught:
        _ = [event async for event in session.events()]
    assert caught.value.classification == "tool_policy_refused"


@pytest.mark.slow
async def test_real_stdio_child_has_private_auth_fixed_flags_and_cleanup(tmp_path, monkeypatch):
    import json
    import sys

    receipt = tmp_path / "receipt.json"
    binary = tmp_path / "copilot"
    binary.write_text(f"""#!{sys.executable}
import json, os, sys
if '--version' in sys.argv:
 print('GitHub Copilot CLI 1.0.86.')
 sys.exit(0)
with open({str(receipt)!r}, 'w') as output:
 json.dump({{'argv': sys.argv, 'auth': os.environ.get('COPILOT_GITHUB_TOKEN'), 'repository_token': os.environ.get('GITHUB_TOKEN'), 'home': os.environ['COPILOT_HOME']}}, output)
for line in sys.stdin:
 frame=json.loads(line)
 method=frame.get('method')
 if method=='initialize': result={{'protocolVersion': 1}}
 elif method=='session/new': result={{'sessionId': 'session-1'}}
 elif method=='session/prompt': result={{'stopReason': 'end_turn'}}
 else: continue
 print(json.dumps({{'jsonrpc':'2.0','id':frame['id'],'result':result}}), flush=True)
""")
    binary.chmod(0o700)
    workspace = tmp_path / "work"
    base = checkout(workspace)
    _, brief = inputs(base)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-secret-only")
    monkeypatch.setenv("GITHUB_TOKEN", "repository-token-must-not-leak")
    session = await CopilotCLIDriver(binary=str(binary)).start(
        brief, workspace, DriverEnv("frozen-model")
    )
    events = [event async for event in session.events()]
    assert events[-1].kind == "completed" and events[-1].usage is None
    received = json.loads(receipt.read_text())
    assert received["auth"] == "copilot-secret-only" and received["repository_token"] is None
    assert "--model=frozen-model" in received["argv"]
    assert "--available-tools=view,edit,create,apply_patch" in received["argv"]
    assert "--deny-tool=shell" in received["argv"] and "--disable-builtin-mcps" in received["argv"]
    assert not any("--allow-all" in value for value in received["argv"])
    from pathlib import Path

    assert not Path(received["home"]).exists()


async def test_apply_patch_permission_validates_every_header_and_normalizes_locations(
    tmp_path, monkeypatch
):
    wire = FakeACP(
        permission={
            "toolCall": {
                "kind": "edit",
                "rawInput": {
                    "input": "*** Begin Patch\n*** Add File: src/new.py\n+value = 1\n*** End Patch\n"
                },
                "locations": [{"path": str(tmp_path / "work/src/new.py")}],
            },
            "options": [{"kind": "allow_once", "optionId": "once"}],
        }
    )
    _, session, _ = await start(tmp_path, monkeypatch, wire)
    events = [event async for event in session.events()]
    assert events[-1].kind == "completed"
    assert session.permission_paths(
        {"input": "*** Begin Patch\n*** Move to: ../outside\n*** End Patch\n"}
    ) == ["../outside"]
    assert not session.policy.permitted("Write", {"file_path": "../outside"})


@pytest.mark.slow
async def test_cancel_during_version_probe_reaps_the_owned_child(tmp_path):
    import os
    import sys
    from forge_lane.drivers.copilot_transport import StdioTransport

    receipt = tmp_path / "pid"
    binary = tmp_path / "copilot"
    binary.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nPath({str(receipt)!r}).write_text(str(os.getpid()))\nwhile True: time.sleep(1)\n"
    )
    binary.chmod(0o700)
    task = asyncio.create_task(StdioTransport.verify_version(str(binary), tmp_path / "private"))
    async with asyncio.timeout(3):
        while not receipt.exists():
            await asyncio.sleep(0.01)
    pid = int(receipt.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_stderr_redaction_survives_chunks_splitting_a_token():
    from types import SimpleNamespace
    from forge_lane.drivers.copilot_transport import StdioTransport

    reader = asyncio.StreamReader()
    emitted = []
    transport = object.__new__(StdioTransport)
    transport.process = SimpleNamespace(stderr=reader)
    transport.token = "synthetic-copilot-token"
    transport.emit = emitted.append
    task = asyncio.create_task(transport.stderr())
    reader.feed_data(b"authorization: Bearer synthetic-copi")
    await asyncio.sleep(0)
    reader.feed_data(b"lot-token\n")
    reader.feed_eof()
    await task
    assert emitted and all("synthetic" not in line and "lot-token" not in line for line in emitted)
    assert "[REDACTED]" in emitted[0]
